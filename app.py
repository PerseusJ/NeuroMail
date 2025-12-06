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
import hashlib
import streamlit.components.v1 as components
import auth_utils

# --- 1. PAGE CONFIG ---
st.set_page_config(
    page_title="NeuroMail Dashboard",
    page_icon="🧠",
    layout="wide",
    initial_sidebar_state="expanded"
)

# --- 2. STUNNING CSS & THEME ---
st.markdown("""
<style>
    /* Global Reset & Fonts */
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;600;800&family=JetBrains+Mono:wght@400;700&display=swap');
    
    .stApp {
        background-color: #0f172a; /* Slate 900 */
        color: #f8fafc;
        font-family: 'Inter', sans-serif;
    }
    
    /* Sidebar Styling */
    section[data-testid="stSidebar"] {
        background-color: #1e293b; /* Slate 800 */
        border-right: 1px solid #334155;
    }
    
    /* Headers & Titles */
    h1, h2, h3 {
        color: #f1f5f9;
        font-weight: 800;
        letter-spacing: -0.5px;
    }
    
    /* Custom Metric Cards */
    .metric-card {
        background: linear-gradient(135deg, #1e293b 0%, #0f172a 100%);
        border: 1px solid #334155;
        border-radius: 16px;
        padding: 20px;
        box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.1), 0 2px 4px -1px rgba(0, 0, 0, 0.06);
        text-align: center;
        transition: transform 0.2s;
    }
    .metric-card:hover {
        transform: translateY(-2px);
        border-color: #64748b;
    }
    .metric-value {
        font-size: 32px;
        font-weight: 800;
        background: -webkit-linear-gradient(#38bdf8, #818cf8);
        -webkit-background-clip: text;
        -webkit-text-fill-color: transparent;
    }
    .metric-label {
        font-size: 14px;
        color: #94a3b8;
        text-transform: uppercase;
        letter-spacing: 1px;
        margin-top: 5px;
    }

    /* Live Pulse Badge */
    .live-badge {
        display: inline-flex;
        align-items: center;
        background-color: rgba(34, 197, 94, 0.1);
        color: #4ade80;
        padding: 6px 12px;
        border-radius: 9999px;
        font-weight: 600;
        font-size: 12px;
        border: 1px solid rgba(34, 197, 94, 0.2);
        animation: pulse-border 2s infinite;
    }
    .dot {
        width: 8px;
        height: 8px;
        background-color: #4ade80;
        border-radius: 50%;
        margin-right: 8px;
        animation: pulse-dot 2s infinite;
    }
    @keyframes pulse-border {
        0% { box-shadow: 0 0 0 0 rgba(74, 222, 128, 0.4); }
        70% { box-shadow: 0 0 0 6px rgba(74, 222, 128, 0); }
        100% { box-shadow: 0 0 0 0 rgba(74, 222, 128, 0); }
    }
    @keyframes pulse-dot {
        0% { opacity: 1; }
        50% { opacity: 0.5; }
        100% { opacity: 1; }
    }
    
    /* Dataframes */
    div[data-testid="stDataFrame"] {
        border: 1px solid #334155;
        border-radius: 12px;
        overflow: hidden;
    }
</style>
""", unsafe_allow_html=True)

# --- 3. CONSTANTS & STATE ---

if 'data' not in st.session_state:
    st.session_state.data = pd.DataFrame()

if 'seen_emails' not in st.session_state:
    st.session_state.seen_emails = set()

if 'monitoring' not in st.session_state: st.session_state.monitoring = False
if 'scan_status' not in st.session_state: st.session_state.scan_status = "Idle"
if 'last_scan_time' not in st.session_state: st.session_state.last_scan_time = None
if 'last_max_id' not in st.session_state: st.session_state.last_max_id = 0
if 'current_user' not in st.session_state: st.session_state.current_user = None
if 'oauth_token' not in st.session_state: st.session_state.oauth_token = None

# --- 4. HELPER FUNCTIONS ---
def get_user_history_file(email_address):
    if not email_address: return None
    safe_name = hashlib.md5(email_address.strip().lower().encode()).hexdigest()
    return f"scan_history_{safe_name}.csv"

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
    body_text = ""
    body_html = ""
    tokens = []
    
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            
            if ctype == "text/plain":
                try: body_text = part.get_payload(decode=True).decode(errors='ignore')
                except: pass
            elif ctype == "text/html":
                try: body_html = part.get_payload(decode=True).decode(errors='ignore')
                except: pass
                
            if part.get_filename():
                fname = part.get_filename().lower()
                if ".pdf" in fname: tokens.append("PDF")
                elif ".jpg" in fname or ".jpeg" in fname or ".png" in fname: tokens.append("IMG")
                elif "invite" in fname: tokens.append("CALENDAR")
    else:
        # Not multipart, payload is body
        try:
            payload = msg.get_payload(decode=True).decode(errors='ignore')
            if msg.get_content_type() == "text/html":
                body_html = payload
            else:
                body_text = payload
        except: pass
        
    # If we found HTML but no Text, use HTML as text (cleaned) for classification
    # If we found Text but no HTML, simple.
    
    final_text_for_model = clean_text(body_text) if body_text else clean_text(re.sub('<[^<]+?>', '', body_html))
    
    return final_text_for_model, body_text, body_html, tokens

def save_history(user_email):
    fname = get_user_history_file(user_email)
    if fname and not st.session_state.data.empty:
        st.session_state.data.to_csv(fname, index=False)

def process_single_email(msg, model, e_id_int):
    sub = safe_decode_header(msg["Subject"])
    snd = str(msg.get("From")).replace("<", "").replace(">", "")
    
    # Extract content (Text for Model, HTML for Display)
    c_b_model, body_plain, body_html, toks = get_email_content(msg)
    
    c_s, c_sub = clean_text(snd), clean_text(sub)
    tok_str = " ".join(toks)
    
    # Deduplication
    sig = f"{c_s}_{c_sub}_{c_b_model[:20]}"
    if sig in st.session_state.seen_emails:
        return "DUPLICATE"

    # Prediction
    full_input = f"{c_s} {c_s} {c_s} {tok_str} {c_sub} {c_b_model}"
    try:
        pred = model.predict([full_input])[0]
        prob = max(model.predict_proba([full_input])[0])
    except:
        # Fallback if model fails or no proba
        pred = "Unknown"
        prob = 0.0

    label_map = {0: "Low", 1: "Medium", 2: "High"}
    priority_label = "Unknown"
    if isinstance(pred, int):
        priority_label = label_map.get(pred, "Unknown")
    else:
        priority_label = str(pred)

    if priority_label == '0': priority_label = "Low"
    if priority_label == '1': priority_label = "Medium"
    if priority_label == '2': priority_label = "High"

    row = {
        "Time": datetime.datetime.now().strftime("%H:%M:%S"),
        "Priority": priority_label,
        "Confidence": prob,
        "Sender": c_s,
        "Subject": c_sub,
        "Tokens": toks,
        "Content": c_b_model[:500], # Short snippet for legacy/debug
        "ContentFull": body_plain,  # Full Plain Text
        "ContentHtml": body_html,   # Full HTML
        "ID": e_id_int
    }
    
    st.session_state.seen_emails.add(sig)
    return row

# --- 5. SCANNING LOGIC ---
def run_scan_cycle(model, server, user, limit, placeholder_metrics, placeholder_table, placeholder_status, placeholder_detail):
    try:
        # REFRESH TOKEN LOGIC
        token_data = st.session_state.get('oauth_token')
        if not token_data:
             raise Exception("Not authenticated")

        if token_data['provider'] == 'google':
            token_data = auth_utils.refresh_google_token(token_data)
            if not token_data: raise Exception("Token refresh failed")
            access_token = token_data['token']
        else:
            token_data = auth_utils.refresh_microsoft_token(token_data)
            if not token_data: raise Exception("Token refresh failed")
            access_token = token_data.get('access_token')
        
        # Update session
        st.session_state['oauth_token'] = token_data

        # Connect with XOAUTH2
        # IMAP authenticate command typically expects base64 encoded SASL response
        auth_str = auth_utils.generate_oauth2_string(user, access_token, base64_encode=True)
        
        mail = imaplib.IMAP4_SSL(server, 993)
        # XOAUTH2 requires specific auth mechanism
        # We pass the auth string directly to the authentication method
        mail.authenticate("XOAUTH2", lambda x: auth_str)
        mail.select("inbox")

        # Search for ALL messages
        _, messages = mail.search(None, 'ALL')
        raw_ids = messages[0].split()
        
        if not raw_ids:
            st.session_state.scan_status = "Inbox Empty"
            mail.logout()
            return

        # Convert to ints and sort descending (newest first)
        all_ids = sorted([int(x) for x in raw_ids], reverse=True)
        
        # Determine mode
        is_live_update = (st.session_state.last_max_id > 0)
        
        processed_count = 0
        
        # ID Selection
        if is_live_update:
             ids_to_process = [x for x in all_ids if x > st.session_state.last_max_id]
             if not ids_to_process:
                 st.session_state.scan_status = "Monitoring (Up to date)"
                 placeholder_status.markdown(f'<div class="live-badge"><div class="dot"></div>LIVE: Monitoring...</div>', unsafe_allow_html=True)
                 mail.logout()
                 return
        else:
            ids_to_process = all_ids # We will iterate through this manually
            st.session_state.scan_status = f"Batch Scanning (Target: {limit})"
            placeholder_status.info(f"Scanning for {limit} new emails...")
        
        new_rows = []
        
        for e_id_int in ids_to_process:
            # Stop if we hit the limit in batch mode
            if not is_live_update and processed_count >= limit:
                break

            # Update high water mark
            if e_id_int > st.session_state.last_max_id:
                st.session_state.last_max_id = e_id_int
            
            try:
                _, msg_data = mail.fetch(str(e_id_int), "(RFC822)")
                for response_part in msg_data:
                    if isinstance(response_part, tuple):
                        msg = email.message_from_bytes(response_part[1])
                        row = process_single_email(msg, model, e_id_int)
                        
                        if row == "DUPLICATE":
                            continue
                        
                        if row:
                            new_rows.append(row)
                            processed_count += 1
                            
                            # Immediate Session Update
                            temp_df = pd.DataFrame([row])
                            st.session_state.data = pd.concat([temp_df, st.session_state.data], ignore_index=True)
                            
                            # Sort
                            sort_map = {"High": 0, "Medium": 1, "Low": 2, "Unknown": 3}
                            st.session_state.data['SortKey'] = st.session_state.data['Priority'].map(sort_map).fillna(3)
                            st.session_state.data = st.session_state.data.sort_values(by=['SortKey', 'Time'], ascending=[True, False]).drop('SortKey', axis=1)
                            
                            save_history(user)
                            
                            # Update UI
                            with placeholder_metrics.container():
                                render_metrics()
                            # Table needs to handle selection state carefully in loop, 
                            # but usually st.data_editor is interactive.
                            # We just redraw the table for new rows.
                            with placeholder_table.container():
                                render_table_with_selection()
                                
            except Exception as e:
                print(f"Error processing email {e_id_int}: {e}")
                continue

        mail.logout()
        st.session_state.last_scan_time = datetime.datetime.now()
        
        if new_rows:
            if is_live_update:
                st.toast(f"Found {len(new_rows)} new emails!", icon="📩")
        elif not is_live_update and processed_count < limit:
             st.warning(f"Only found {processed_count} valid new emails (checked {len(all_ids)}).")
        
    except Exception as e:
        st.error(f"Connection Error: {e}")
        st.session_state.monitoring = False

# --- 6. UI COMPONENTS ---
def render_metrics():
    df = st.session_state.data
    if df.empty:
        h, m, l = 0, 0, 0
    else:
        h = len(df[df['Priority'] == "High"])
        m = len(df[df['Priority'] == "Medium"])
        l = len(df[df['Priority'] == "Low"])
    
    c1, c2, c3 = st.columns(3)
    with c1:
        st.markdown(f"""<div class="metric-card"><div class="metric-value" style="color:#ef4444">{h}</div><div class="metric-label">High Priority</div></div>""", unsafe_allow_html=True)
    with c2:
        st.markdown(f"""<div class="metric-card"><div class="metric-value" style="color:#f59e0b">{m}</div><div class="metric-label">Medium Priority</div></div>""", unsafe_allow_html=True)
    with c3:
        st.markdown(f"""<div class="metric-card"><div class="metric-value" style="color:#3b82f6">{l}</div><div class="metric-label">Low Priority</div></div>""", unsafe_allow_html=True)

def render_table_with_selection():
    df = st.session_state.data
    if df.empty:
        st.info("No emails scanned yet.")
        return None

    # Hide bulky columns
    table_df = df.drop(columns=['ContentFull', 'ContentHtml', 'Content', 'ID'], errors='ignore')
    
    # Interactive Table
    selected_rows = st.dataframe(
        table_df,
        column_order=("Priority", "Confidence", "Time", "Sender", "Subject", "Tokens"),
        column_config={
            "Priority": st.column_config.TextColumn(width="small"),
            "Confidence": st.column_config.ProgressColumn(format="%.2f", min_value=0, max_value=1, width="small"),
            "Subject": st.column_config.TextColumn(width="large"),
            "Sender": st.column_config.TextColumn(width="medium"),
            "Tokens": st.column_config.ListColumn(width="small"),
        },
        use_container_width=True,
        hide_index=True,
        height=400,
        selection_mode="single-row",
        on_select="rerun" 
    )
    
    # Return the index of the selected row if any
    if selected_rows and len(selected_rows.selection.rows) > 0:
        return selected_rows.selection.rows[0]
    return None

def render_detail_panel(selected_idx):
    if selected_idx is None:
        st.info("Select an email from the list to view details.")
        return

    # Retrieve row
    try:
        # iloc works on position, assuming index hasn't been messed with or we use reset_index
        # st.dataframe selection returns row index relative to displayed dataframe.
        # We displayed st.session_state.data directly (just dropped cols).
        # So the index should match.
        row = st.session_state.data.iloc[selected_idx]
    except:
        st.warning("Selection out of sync. Please re-select.")
        return

    st.markdown("### 📧 Email Content")
    
    # Header Info
    c1, c2 = st.columns([3, 1])
    with c1:
        st.markdown(f"**Subject:** {row['Subject']}")
        st.markdown(f"**From:** {row['Sender']}")
    with c2:
        st.caption(f"Time: {row['Time']}")
        st.caption(f"Priority: {row['Priority']}")

    st.divider()

    # Body Content
    # Prefer HTML if available, else Plain Text
    html_content = row.get("ContentHtml")
    plain_content = row.get("ContentFull")
    
    # If NaN/None, treat as empty string
    if pd.isna(html_content): html_content = ""
    if pd.isna(plain_content): plain_content = ""

    if html_content:
        # Render HTML in a secure iframe
        components.html(html_content, height=600, scrolling=True)
    elif plain_content:
        st.text_area("Message Body", plain_content, height=400)
    else:
        st.caption("No content available.")

# --- 7. MAIN LAYOUT ---
def main():
    # --- OAUTH CALLBACK HANDLER ---
    if 'code' in st.query_params:
        code = st.query_params['code']
        state = st.query_params.get('state', 'google')
        
        token_data = None
        try:
            if state == 'google':
                token_data = auth_utils.get_google_token_from_code(code)
            elif state == 'microsoft':
                token_data = auth_utils.get_microsoft_token_from_code(code)
            
            if token_data:
                st.session_state['oauth_token'] = token_data
                st.session_state['current_user'] = token_data['email']
                st.success("Login Successful!")
                # Reset URL
                st.query_params.clear()
                st.rerun()
        except Exception as e:
            st.error(f"Login Error: {e}")

    with st.sidebar:
        st.title("🧠 NeuroMail")
        st.caption("AI-Powered Email Intelligence")
        
        st.markdown("### ⚙️ Configuration")
        
        model_path = "email_model.pkl"
        uploaded_file = None
        if os.path.exists(model_path):
            st.success("Model Loaded", icon="✅")
            uploaded_file = model_path
        else:
            uploaded_file = st.file_uploader("Upload Model (.pkl)", type="pkl")

        with st.expander("📧 Login", expanded=True):
            if not st.session_state.get('oauth_token'):
                st.markdown("Please log in to scan your emails.")
                
                # Google Login
                g_url = auth_utils.get_google_auth_url()
                if g_url: 
                    st.link_button("Login with Google", g_url, use_container_width=True)
                else:
                    st.caption("Google credentials not configured.")
                
                st.write("") # Spacer

                # Microsoft Login
                m_url = auth_utils.get_microsoft_auth_url()
                if m_url:
                    st.link_button("Login with Microsoft", m_url, use_container_width=True)
                else:
                    st.caption("Microsoft credentials not configured.")
            else:
                st.success(f"Logged in as: {st.session_state.current_user}")
                if st.button("Logout", use_container_width=True):
                    st.session_state.oauth_token = None
                    st.session_state.current_user = None
                    st.session_state.data = pd.DataFrame()
                    st.session_state.monitoring = False
                    st.rerun()

        # --- USER SESSION LOGIC ---
        if st.session_state.current_user:
             # Load User Specific File
             user_file = get_user_history_file(st.session_state.current_user)
             if user_file and os.path.exists(user_file) and st.session_state.data.empty:
                 try:
                     st.session_state.data = pd.read_csv(user_file)
                     # Re-populate seen cache
                     for _, row in st.session_state.data.iterrows():
                         sig = f"{row.get('Sender')}_{row.get('Subject')}_{str(row.get('Content'))[:20]}"
                         st.session_state.seen_emails.add(sig)
                 except:
                     pass

        st.markdown("---")
        scan_limit = st.slider("Batch Scan Size (Newest)", 10, 1000, 50)
        
        col1, col2 = st.columns(2)
        with col1:
            if st.button("🔴 Stop", use_container_width=True):
                st.session_state.monitoring = False
                st.rerun()
        with col2:
            start_btn = st.button("🟢 Start", use_container_width=True)
            if start_btn:
                if not st.session_state.get('oauth_token'):
                    st.error("Please login first!")
                elif not uploaded_file:
                    st.error("Model required!")
                else:
                    st.session_state.monitoring = True
                    st.rerun()

        st.markdown("---")
        
        # Clear History (User Scoped)
        if st.button("🗑️ Clear History", use_container_width=True):
            st.session_state.data = pd.DataFrame()
            st.session_state.seen_emails = set()
            st.session_state.last_max_id = 0
            
            if st.session_state.current_user:
                u_file = get_user_history_file(st.session_state.current_user)
                if u_file and os.path.exists(u_file):
                    os.remove(u_file)
            st.rerun()
            
        if not st.session_state.data.empty:
            # Do not include raw HTML in export unless requested, keep it light
            export_df = st.session_state.data.drop(columns=['ContentHtml', 'ContentFull'], errors='ignore')
            csv = export_df.to_csv(index=False).encode('utf-8')
            st.download_button("💾 Download CSV", csv, "email_report.csv", "text/csv", use_container_width=True)

    st.title("Live Inbox Monitor")
    
    if st.session_state.current_user:
        st.caption(f"Logged in as: {st.session_state.current_user}")
    else:
        st.info("Please log in using the sidebar.")

    metrics_placeholder = st.empty()
    with metrics_placeholder.container():
        render_metrics()
    
    st.divider()
    
    status_col, _ = st.columns([1, 3])
    status_placeholder = status_col.empty()
    
    if st.session_state.monitoring:
        status_placeholder.markdown(f'<div class="live-badge"><div class="dot"></div>LIVE: Active</div>', unsafe_allow_html=True)
    else:
        status_placeholder.markdown(f'<div style="color: #64748b; font-weight:600">● Inactive</div>', unsafe_allow_html=True)
    
    # --- SELECTABLE TABLE ---
    table_placeholder = st.empty()
    selected_row_idx = None
    with table_placeholder.container():
        selected_row_idx = render_table_with_selection()

    st.markdown("---")
    
    # --- DETAIL PANEL ---
    detail_placeholder = st.empty()
    with detail_placeholder.container():
        render_detail_panel(selected_row_idx)

    # --- BACKGROUND WORKER ---
    if st.session_state.monitoring and uploaded_file and st.session_state.get('oauth_token'):
        try:
            if isinstance(uploaded_file, str):
                model = joblib.load(uploaded_file)
            else:
                model = joblib.load(uploaded_file)
            
            # Determine server based on provider
            provider = st.session_state.oauth_token['provider']
            server = "imap.gmail.com" if provider == 'google' else "outlook.office365.com"
            
            run_scan_cycle(
                model, server, st.session_state.current_user, 
                scan_limit, metrics_placeholder, table_placeholder, status_placeholder, detail_placeholder
            )
            
            time.sleep(5)
            st.rerun()
            
        except Exception as e:
            st.error(f"Runtime Error: {e}")
            st.session_state.monitoring = False

if __name__ == "__main__":
    main()
