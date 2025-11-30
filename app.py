import streamlit as st
import imaplib
import email
from email.header import decode_header
import joblib
import pandas as pd
import re
import time
import datetime
import threading
import os

st.set_page_config(page_title="NeuroMail Render", page_icon="🧠", layout="wide")

# --- 1. THE GHOST BOT (Background Thread) ---
# We use cache_resource so this object stays alive in memory
# as long as the server is running.
@st.cache_resource
class BackgroundScanner:
    def __init__(self):
        self.data = pd.DataFrame()
        self.is_running = False
        self.last_update = "System Start"
        self.seen_emails = set()
        self.max_id_seen = 0
        self.status = "Idle"
        
    def start(self, model, server, user, password, backlog_limit):
        if self.is_running: return
        self.is_running = True
        # Launch the separate thread
        thread = threading.Thread(target=self._loop, args=(model, server, user, password, backlog_limit))
        thread.start()

    def stop(self):
        self.is_running = False
        self.status = "Stopped"

    def _loop(self, model, server, user, password, backlog_limit):
        label_map = {0: "Low", 1: "Medium", 2: "High"}
        
        while self.is_running:
            try:
                # Explicit Port 993 for Render Firewalls
                mail = imaplib.IMAP4_SSL(server, 993)
                mail.login(user, password)
                mail.select("inbox")
                
                _, messages = mail.search(None, 'UNSEEN')
                raw_ids = messages[0].split()
                
                if raw_ids:
                    email_ids = sorted([int(x) for x in raw_ids], reverse=True)
                    ids_to_process = []

                    if self.max_id_seen == 0:
                        ids_to_process = email_ids[:backlog_limit]
                        if ids_to_process: self.max_id_seen = max(ids_to_process)
                    else:
                        ids_to_process = [x for x in email_ids if x > self.max_id_seen]
                        if ids_to_process: self.max_id_seen = max(ids_to_process)
                    
                    if ids_to_process:
                        self.status = f"Processing {len(ids_to_process)} new emails..."
                        
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
                                        
                                        new_df = pd.DataFrame([new_row])
                                        self.data = pd.concat([new_df, self.data], ignore_index=True)
                                        self.last_update = datetime.datetime.now().strftime("%H:%M:%S")
                            except: continue
                    else:
                        self.status = f"Monitoring... (Up to date as of {datetime.datetime.now().strftime('%H:%M:%S')})"
                else:
                    self.status = "Inbox Empty (No Unread)"

                mail.logout()
                
                # Sleep 15s between checks
                for _ in range(15):
                    if not self.is_running: break
                    time.sleep(1)
                    
            except Exception as e:
                self.status = f"Error: {e}"
                time.sleep(30)

# --- 2. HELPERS ---
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

# --- 3. UI SETUP ---
scanner = BackgroundScanner()

st.markdown("""
<style>
    .stApp { background-color: #0e1117; color: #fafafa; }
    section[data-testid="stSidebar"] { background-color: #161b22; }
    .status-box {
        padding: 10px; border-radius: 5px; background-color: #1e293b; 
        border: 1px solid #30363d; font-family: monospace; margin-bottom: 20px;
    }
</style>
""", unsafe_allow_html=True)

with st.sidebar:
    st.title("🧠 NeuroMail Render")
    st.markdown("### 24/7 Background Service")
    
    model_path = "email_model.pkl"
    if os.path.exists(model_path):
        st.success(f"Brain Loaded")
        model = joblib.load(model_path)
    else:
        st.error("Missing 'email_model.pkl'")
        st.stop()

    with st.expander("Credentials", expanded=True):
        imap_server = st.selectbox("Provider", ["imap.gmail.com", "outlook.office365.com"])
        email_user = st.text_input("Email")
        email_pass = st.text_input("App Password", type="password")

    max_scan = st.number_input("Backlog Limit", value=50)

    c1, c2 = st.columns(2)
    if c1.button("🟢 START"):
        if email_user and email_pass:
            scanner.start(model, imap_server, email_user, email_pass, max_scan)
            st.rerun()
    if c2.button("🔴 STOP"):
        scanner.stop()
        st.rerun()
        
    if st.button("Clear Data"):
        scanner.data = pd.DataFrame()
        scanner.seen_emails = set()
        st.rerun()

# --- 4. DASHBOARD ---
st.markdown(f"### Status: {'🟢 Running' if scanner.is_running else '🔴 Stopped'}")
st.markdown(f"<div class='status-box'>{scanner.status} | Last Update: {scanner.last_update}</div>", unsafe_allow_html=True)

# Refresh UI if running to show new data
if scanner.is_running:
    time.sleep(5)
    st.rerun()

if not scanner.data.empty:
    df = scanner.data
    
    # Sort
    sort_map = {"High": 0, "Medium": 1, "Low": 2, "Unknown": 3}
    df['SortKey'] = df['Priority'].map(sort_map)
    df = df.sort_values(by=['Time', 'SortKey'], ascending=[False, True]).drop('SortKey', axis=1)
    
    c1, c2, c3 = st.columns(3)
    c1.metric("High", len(df[df['Priority']=="High"]))
    c2.metric("Medium", len(df[df['Priority']=="Medium"]))
    c3.metric("Low", len(df[df['Priority']=="Low"]))
    
    st.dataframe(df, use_container_width=True, hide_index=True)
