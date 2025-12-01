import streamlit as st
import imaplib
import smtplib
import email
from email.header import decode_header
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders
import joblib
import pandas as pd
import re
import time
import datetime
import threading
import os
import io

st.set_page_config(page_title="NeuroMail Pro", page_icon="🧠", layout="wide")

# --- CSS STYLING ---
st.markdown("""
<style>
    .stApp { background-color: #0e1117; color: #fafafa; font-family: 'Inter', sans-serif; }
    section[data-testid="stSidebar"] { background-color: #161b22; border-right: 1px solid #30363d; }
    .status-box {
        padding: 12px; border-radius: 8px; background-color: #1e293b; 
        border: 1px solid #334155; font-family: monospace; margin-bottom: 20px; color: #e2e8f0;
    }
    .live-badge {
        background-color: #22c55e; color: white; padding: 4px 12px; 
        border-radius: 12px; font-weight: bold; font-size: 12px; animation: pulse 2s infinite;
    }
    @keyframes pulse { 0% { opacity: 1; } 50% { opacity: 0.5; } 100% { opacity: 1; } }
</style>
""", unsafe_allow_html=True)

# --- HELPER FUNCTIONS ---
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
    else:
        try: body = msg.get_payload(decode=True).decode(errors='ignore')
        except: pass
    return clean_text(body), tokens

# --- BACKGROUND WORKER ---
@st.cache_resource
class BackgroundScanner:
    def __init__(self):
        self.data = pd.DataFrame()
        self.buffer = []
        self.is_running = False
        self.last_update = "System Ready"
        self.status_log = "Idle"
        self.seen_emails = set()
        self.max_id_seen = 0
        
    def start(self, model, server, user, password, backlog_limit, autosave_limit):
        if self.is_running: return
        self.is_running = True
        self.status_log = "Starting Background Thread..."
        thread = threading.Thread(target=self._loop, args=(model, server, user, password, backlog_limit, autosave_limit))
        thread.daemon = True # Ensures thread dies if server restarts
        thread.start()

    def stop(self):
        self.is_running = False
        self.status_log = "Stopping..."

    def clear(self):
        self.data = pd.DataFrame()
        self.buffer = []
        self.status_log = "History Cleared"

    def _send_autosave(self, buffer_data, user, password, server):
        try:
            df = pd.DataFrame(buffer_data)
            csv_buffer = io.StringIO()
            df.to_csv(csv_buffer, index=False)
            
            smtp_server = "smtp.gmail.com" if "gmail" in server else "smtp.office365.com"
            msg = MIMEMultipart()
            msg['From'] = user
            msg['To'] = user
            msg['Subject'] = f"NeuroMail Auto-Save: {len(buffer_data)} Items"
            
            part = MIMEBase('application', "octet-stream")
            part.set_payload(csv_buffer.getvalue())
            encoders.encode_base64(part)
            part.add_header('Content-Disposition', f'attachment; filename="backup.csv"')
            msg.attach(part)

            s = smtplib.SMTP(smtp_server, 587)
            s.starttls()
            s.login(user, password)
            s.sendmail(user, user, msg.as_string())
            s.quit()
            return True
        except Exception as e:
            print(f"Email Error: {e}")
            return False

    def _loop(self, model, server, user, password, backlog_limit, autosave_limit):
        label_map = {0: "Low", 1: "Medium", 2: "High"}
        
        while self.is_running:
            try:
                # 1. Connect
                self.status_log = "Connecting to IMAP..."
                mail = imaplib.IMAP4_SSL(server, 993)
                mail.login(user, password)
                mail.select("inbox")
                
                # 2. Search
                _, messages = mail.search(None, 'UNSEEN')
                raw_ids = messages[0].split()
                
                if not raw_ids:
                    self.status_log = f"No Unread Emails. Waiting... (Last check: {datetime.datetime.now().strftime('%H:%M:%S')})"
                
                else:
                    email_ids = sorted([int(x) for x in raw_ids], reverse=True)
                    ids_to_process = []

                    # 3. Filter Logic
                    if self.max_id_seen == 0:
                        ids_to_process = email_ids[:backlog_limit]
                        if ids_to_process: 
                            self.max_id_seen = max(ids_to_process)
                            self.status_log = f"Initial Scan: Processing {len(ids_to_process)} backlog emails..."
                    else:
                        ids_to_process = [x for x in email_ids if x > self.max_id_seen]
                        if ids_to_process: 
                            self.max_id_seen = max(ids_to_process)
                            self.status_log = f"Found {len(ids_to_process)} NEW emails!"
                        else:
                            self.status_log = f"Monitoring... (Up to date)"

                    # 4. Process Emails
                    if ids_to_process:
                        for e_id_int in ids_to_process:
                            if not self.is_running: break
                            try:
                                e_id = str(e_id_int)
                                _, msg_data = mail.fetch(e_id, "(RFC822)")
                                for response_part in msg_data:
                                    if isinstance(response_part, tuple):
                                        msg = email.message_from_bytes(response_part[1])
                                        sub = safe_decode_header(msg["Subject"])
                                        snd = str(msg.get("From")).replace("<", "").replace(">", "")
                                        bod, toks = get_email_content(msg)
                                        
                                        c_s, c_sub, c_b = clean_text(snd), clean_text(sub), clean_text(bod)
                                        tok_str = " ".join(toks)
                                        full_input = f"{c_s} {c_s} {c_s} {tok_str} {c_sub} {c_b}"
                                        
                                        pred = model.predict([full_input])[0]
                                        prob = max(model.predict_proba([full_input])[0])
                                        
                                        new_row = {
                                            "Time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
                                            "Priority": label_map.get(pred, "Unknown"),
                                            "Confidence": prob,
                                            "Sender": c_s,
                                            "Subject": c_sub,
                                            "Tokens": toks
                                        }
                                        
                                        # UPDATE GLOBAL DATA
                                        new_df = pd.DataFrame([new_row])
                                        self.data = pd.concat([new_df, self.data], ignore_index=True)
                                        self.buffer.append(new_row)
                                        self.last_update = datetime.datetime.now().strftime("%H:%M:%S")

                                        # Auto-Save Logic
                                        if len(self.buffer) >= autosave_limit:
                                            self.status_log = "Sending Auto-Save Email..."
                                            if self._send_autosave(self.buffer, user, password, server):
                                                self.buffer = []
                                                
                            except Exception: continue
                
                mail.logout()
                
                # Sleep Loop (Keeps checking every 10s)
                for _ in range(10):
                    if not self.is_running: break
                    time.sleep(1)
                    
            except Exception as e:
                self.status_log = f"Error: {str(e)[:50]}..."
                time.sleep(30)

scanner = BackgroundScanner()

# --- SIDEBAR ---
with st.sidebar:
    st.title("🧠 NeuroMail Pro")
    
    model_path = "email_model.pkl"
    if os.path.exists(model_path):
        st.success("✅ Brain Loaded")
        model = joblib.load(model_path)
    else:
        st.error("Missing model file!")
        st.stop()

    with st.expander("Credentials", expanded=True):
        imap_server = st.selectbox("Provider", ["imap.gmail.com", "outlook.office365.com"])
        email_user = st.text_input("Email")
        email_pass = st.text_input("App Password", type="password")

    st.markdown("---")
    backlog = st.number_input("Initial Scan Limit", value=50)
    autosave = st.number_input("Auto-Save Limit", value=50)

    c1, c2 = st.columns(2)
    if c1.button("🟢 START"):
        if email_user and email_pass:
            scanner.start(model, imap_server, email_user, email_pass, backlog, autosave)
            st.rerun()
    if c2.button("🔴 STOP"):
        scanner.stop()
        st.rerun()
    
    if st.button("Clear Dashboard"):
        scanner.clear()
        st.rerun()

# --- MAIN DASHBOARD ---
st.markdown(f"### Status: {'🟢 Running' if scanner.is_running else '🔴 Stopped'}")

# Dynamic Status Box
st.markdown(f"""
<div class='status-box'>
    <b>LOG:</b> {scanner.status_log}<br>
    <b>LAST UPDATE:</b> {scanner.last_update}
</div>
""", unsafe_allow_html=True)

# --- DATA DISPLAY ---
if not scanner.data.empty:
    df = scanner.data.copy()
    
    # Sort Logic
    sort_map = {"High": 0, "Medium": 1, "Low": 2, "Unknown": 3}
    df['SortKey'] = df['Priority'].map(sort_map)
    df = df.sort_values(by=['Time', 'SortKey'], ascending=[False, True]).drop('SortKey', axis=1)

    # Metrics
    c1, c2, c3 = st.columns(3)
    c1.metric("High", len(df[df['Priority']=="High"]))
    c2.metric("Medium", len(df[df['Priority']=="Medium"]))
    c3.metric("Low", len(df[df['Priority']=="Low"]))
    
    st.dataframe(
        df,
        column_order=("Time", "Priority", "Confidence", "Sender", "Subject", "Tokens"),
        column_config={
            "Priority": st.column_config.Column(width="small"),
            "Confidence": st.column_config.ProgressColumn(format="%.2f", min_value=0, max_value=1),
            "Subject": st.column_config.TextColumn(width="large"),
        },
        use_container_width=True,
        hide_index=True
    )
else:
    st.info("Dashboard empty. Click Start to begin scanning.")

# --- UI AUTO-REFRESH ---
# This is the "Consumer" that updates the screen
if scanner.is_running:
    time.sleep(5) # Wait 5 seconds
    st.rerun()    # Refresh page to see new data from the thread
