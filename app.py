import streamlit as st
import imaplib
import email
from email.header import decode_header
import joblib
import pandas as pd
import re
import time
import datetime
import os

# --- 1. PAGE CONFIG ---
st.set_page_config(
    page_title="NeuroMail Live",
    page_icon="📡",
    layout="wide",
    initial_sidebar_state="expanded"
)

# --- 2. CSS STYLING ---
st.markdown("""
<style>
    @import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;700&family=Inter:wght@400;600&display=swap');
    .stApp { background-color: #0e1117; color: #fafafa; font-family: 'Inter', sans-serif; }
    section[data-testid="stSidebar"] { background-color: #161b22; border-right: 1px solid #30363d; }
    
    .live-badge {
        background-color: #22c55e; color: white; padding: 5px 10px; 
        border-radius: 12px; font-weight: bold; font-size: 12px; animation: pulse 2s infinite;
    }
    @keyframes pulse { 0% { opacity: 1; } 50% { opacity: 0.5; } 100% { opacity: 1; } }
    
    div[data-testid="metric-container"] {
        background-color: #1e293b; padding: 10px; border-radius: 8px; border: 1px solid #334155;
    }
</style>
""", unsafe_allow_html=True)

# --- 3. HISTORY FILE ---
HISTORY_FILE = "scan_history.csv"

def load_history():
    if os.path.exists(HISTORY_FILE):
        try:
            return pd.read_csv(HISTORY_FILE)
        except:
            return pd.DataFrame()
    return pd.DataFrame()

def save_row_to_disk(row_dict):
    df = pd.DataFrame([row_dict])
    header = not os.path.exists(HISTORY_FILE)
    df.to_csv(HISTORY_FILE, mode='a', header=header, index=False)

def clear_disk_history():
    if os.path.exists(HISTORY_FILE):
        os.remove(HISTORY_FILE)

# --- 4. STATE ---
if 'data' not in st.session_state: st.session_state.data = load_history()
if 'monitoring' not in st.session_state: st.session_state.monitoring = False
if 'seen_emails' not in st.session_state: st.session_state.seen_emails = set()
if 'last_max_id' not in st.session_state: st.session_state.last_max_id = 0

# Re-populate seen cache from history
if not st.session_state.data.empty:
    for index, row in st.session_state.data.iterrows():
        sig = f"{row['Sender']}_{row['Subject']}_{row['Content'][:20]}"
        st.session_state.seen_emails.add(sig)

# --- 5. HELPERS ---
def clean_text(text):
    if text is None: return ""
    if isinstance(text, bytes): text = text.decode(errors='ignore')
    text = str(text).replace('"', '').replace("'", "").replace("\n", " ").replace("\t", " ")
    return re.sub(' +', ' ', text).strip()

def safe_decode_header(header_value):
    if not header_value: return "No Subject"
    try:
        headers = decode_header(header_value)
        parts = []
        for content, encoding in headers:
            if isinstance(content, bytes):
                parts.append(content.decode(encoding or 'utf-8', errors='ignore'))
            else:
                parts.append(str(content))
        return "".join(parts)
    except: return str(header_value)

def get_email_content(msg):
    body = ""
    tokens = []
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                try: body = part.get_payload(decode=True).decode(errors='ignore')
                except: pass
            if part.get_filename(): 
                fname = part.get_filename().lower()
                if ".pdf" in fname: tokens.append("PDF")
                elif ".jpg" in fname or ".png" in fname: tokens.append("IMG")
                elif "invite" in fname: tokens.append("CALENDAR")
    else:
        try: body = msg.get_payload(decode=True).decode(errors='ignore')
        except: pass
    return clean_text(body), tokens

def sort_dataframe(df):
    if df.empty: return df
    sort_map = {"High": 1, "Medium": 2, "Low": 3, "Unknown": 4}
    df['SortKey'] = df['Priority'].map(sort_map)
    df = df.sort_values(by=['SortKey', 'Time'], ascending=[True, False]).drop('SortKey', axis=1)
    return df

# --- 6. SCANNING LOGIC ---
def scan_inbox(model, server, user, password, limit, table_placeholder, metrics_placeholder, progress_bar, status_text):
    try:
        mail = imaplib.IMAP4_SSL(server, 993)
        mail.login(user, password)
        mail.select("inbox")
        
        _, messages = mail.search(None, 'UNSEEN')
        raw_ids = messages[0].split()
        
        if not raw_ids:
            status_text.info("Inbox is empty (No Unread Mails).")
            mail.logout()
            return

        email_ids = sorted([int(x) for x in raw_ids], reverse=True)
        ids_to_process = []

        # HIGH WATER MARK LOGIC
        if st.session_state.last_max_id == 0:
            ids_to_process = email_ids # Get all, we filter by count below
        else:
            ids_to_process = [x for x in email_ids if x > st.session_state.last_max_id]
        
        if not ids_to_process:
            status_text.text(f"Monitoring... (Up to date)")
            mail.logout()
            return

        label_map = {0: "Low", 1: "Medium", 2: "High"}
        emails_processed = 0
        
        # Initial Display Total (for progress bar)
        total_to_scan = limit if st.session_state.last_max_id == 0 else len(ids_to_process)
        status_text.markdown(f"**Found new emails.** Scanning...")

        for i, e_id_int in enumerate(ids_to_process):
            # HARD STOP logic for Initial Scan
            if st.session_state.last_max_id == 0 and emails_processed >= limit:
                # Set watermark to the highest ID we successfully grabbed
                st.session_state.last_max_id = max(ids_to_process[:limit])
                break
            
            # Update watermark dynamically for live mode
            if e_id_int > st.session_state.last_max_id:
                st.session_state.last_max_id = e_id_int

            try:
                # Update Progress
                prog = min((emails_processed + 1) / total_to_scan, 1.0)
                progress_bar.progress(prog)
                
                e_id = str(e_id_int)
                _, msg_data = mail.fetch(e_id, "(RFC822)")
                
                success = False
                for response_part in msg_data:
                    if isinstance(response_part, tuple):
                        msg = email.message_from_bytes(response_part[1])
                        sub = safe_decode_header(msg["Subject"])
                        snd = str(msg.get("From")).replace("<", "").replace(">", "")
                        bod, toks = get_email_content(msg)
                        
                        c_s, c_sub, c_b = clean_text(snd), clean_text(sub), clean_text(bod)
                        tok_str = " ".join(toks)
                        
                        # Dedupe by signature
                        sig = f"{c_s}_{c_sub}_{c_b[:20]}"
                        if sig in st.session_state.seen_emails:
                            continue
                        st.session_state.seen_emails.add(sig)

                        # Predict
                        full_input = f"{c_s} {c_s} {c_s} {tok_str} {c_sub} {c_b}"
                        pred = model.predict([full_input])[0]
                        prob = max(model.predict_proba([full_input])[0])
                        
                        new_row = {
                            "Time": datetime.datetime.now().strftime("%H:%M"),
                            "Priority": label_map.get(pred, "Unknown"),
                            "Confidence": prob,
                            "Sender": c_s,
                            "Subject": c_sub,
                            "Tokens": toks,
                            "Content": c_b[:500]
                        }
                        
                        save_row_to_disk(new_row)
                        
                        new_df = pd.DataFrame([new_row])
                        st.session_state.data = pd.concat([new_df, st.session_state.data], ignore_index=True)
                        st.session_state.data = sort_dataframe(st.session_state.data)
                        
                        with table_placeholder.container():
                            display_df = st.session_state.data.drop(columns=['Content'], errors='ignore')
                            render_table(display_df)
                        with metrics_placeholder.container():
                            render_metrics(st.session_state.data)
                            
                        success = True

                if success:
                    emails_processed += 1

            except Exception:
                continue

        mail.logout()
        
    except Exception as e:
        st.error(f"Connection Error: {e}")

def render_metrics(df):
    c1, c2, c3 = st.columns(3)
    if not df.empty:
        c1.metric("High Priority", len(df[df['Priority'] == "High"]))
        c2.metric("Medium Priority", len(df[df['Priority'] == "Medium"]))
        c3.metric("Low Priority", len(df[df['Priority'] == "Low"]))
    else:
        c1.metric("High", 0)
        c2.metric("Medium", 0)
        c3.metric("Low", 0)

def render_table(df):
    st.dataframe(
        df,
        column_order=("Priority", "Confidence", "Time", "Sender", "Subject", "Tokens"),
        column_config={
            "Priority": st.column_config.Column(width="small"),
            "Confidence": st.column_config.ProgressColumn(format="%.2f", min_value=0, max_value=1, width="small"),
            "Subject": st.column_config.TextColumn(width="large"),
            "Time": st.column_config.TextColumn(width="small"),
        },
        use_container_width=True,
        hide_index=True
    )

# --- 7. SIDEBAR ---
with st.sidebar:
    st.title("🧠 NeuroMail Live")
    
    # Auto-Load Logic
    model_path = "email_model.pkl"
    uploaded_file = None
    
    if os.path.exists(model_path):
        st.success("✅ Brain Detected on Server")
        uploaded_file = model_path
    else:
        uploaded_file = st.file_uploader("Upload Model", type="pkl")
    
    with st.expander("Credentials", expanded=True):
        imap_server = st.selectbox("Provider", ["imap.gmail.com", "outlook.office365.com"])
        email_user = st.text_input("Email")
        email_pass = st.text_input("App Password", type="password")
    
    st.markdown("---")
    backlog_limit = st.number_input("Initial Scan Limit", min_value=10, max_value=1000, value=50)
    
    col1, col2 = st.columns(2)
    with col1:
        if st.button("🔴 STOP"):
            st.session_state.monitoring = False
            st.rerun()
    with col2:
        if st.button("🟢 START"):
            if email_user and email_pass:
                st.session_state.monitoring = True
                # Note: We do NOT clear history here, allowing it to resume from disk
                st.rerun()
            else:
                st.error("Missing Info")
    
    # Download Button
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE, "rb") as f:
            st.download_button("💾 Download Report", f, file_name="email_report.csv", mime="text/csv")
    
    if st.button("Clear History"):
        clear_disk_history()
        st.session_state.data = pd.DataFrame()
        st.session_state.seen_emails = set()
        st.rerun()

# --- 8. MAIN ---
st.markdown("## 📡 Live Inbox Monitor")

metrics_placeholder = st.empty()
render_metrics(st.session_state.data)

st.divider()

status_text = st.empty()
progress_bar = st.empty()
table_placeholder = st.empty()

if not st.session_state.data.empty:
    with table_placeholder.container():
        # Hide content column
        display_df = st.session_state.data.drop(columns=['Content'], errors='ignore')
        render_table(display_df)
else:
    table_placeholder.info("History empty. Waiting for scan...")

# --- 9. LOOP ---
if st.session_state.monitoring:
    status_text.markdown('<span class="live-badge">● LIVE: Scanning...</span>', unsafe_allow_html=True)
    
    try:
        if isinstance(uploaded_file, str):
            model = joblib.load(uploaded_file)
        else:
            model = joblib.load(uploaded_file)
            
        # FIXED: Added status_text to the call
        scan_inbox(model, imap_server, email_user, email_pass, backlog_limit, 
                   table_placeholder, metrics_placeholder, progress_bar, status_text)
    except Exception as e:
        st.error(f"Init Error: {e}")
        st.session_state.monitoring = False

    time.sleep(10)
    st.rerun()
