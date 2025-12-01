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

# --- 1. PAGE CONFIG ---
st.set_page_config(
    page_title="NeuroMail Pro",
    page_icon="🧠",
    layout="wide",
    initial_sidebar_state="expanded"
)

# --- 2. CSS STYLING ---
st.markdown("""
<style>
    @import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;700&family=Inter:wght@400;600&display=swap');
    .stApp { background-color: #0e1117; color: #fafafa; font-family: 'Inter', sans-serif; }
    section[data-testid="stSidebar"] { background-color: #161b22; border-right: 1px solid #30363d; }
    .status-box {
        padding: 10px; border-radius: 5px; background-color: #1e293b; 
        border: 1px solid #334155; font-family: monospace; margin-bottom: 20px; color: #e2e8f0;
    }
    .live-badge {
        background-color: #22c55e; color: white; padding: 5px 10px; 
        border-radius: 12px; font-weight: bold; font-size: 12px; animation: pulse 2s infinite;
    }
    @keyframes pulse { 0% { opacity: 1; } 50% { opacity: 0.5; } 100% { opacity: 1; } }
</style>
""", unsafe_allow_html=True)

# --- 3. BACKGROUND SCANNER CLASS ---
# This keeps running even if you close the browser
@st.cache_resource
class BackgroundScanner:
    def __init__(self):
        self.data = pd.DataFrame()
        self.buffer = []
        self.is_running = False
        self.last_update = "System Start"
        self.seen_emails = set()
        self.max_id_seen = 0
        self.status = "Idle"
        
    def start(self, model, server, user, password, backlog_limit, autosave_limit):
        if self.is_running: return
        self.is_running = True
        thread = threading.Thread(target=self._loop, args=(model, server, user, password, backlog_limit, autosave_limit))
        thread.start()

    def stop(self):
        self.is_running = False
        self.status = "Stopped"

    def _send_autosave_email(self, buffer_data, user, password, server_type):
        try:
            df = pd.DataFrame(buffer_data)
            csv_buffer = io.StringIO()
            df.to_csv(csv_buffer, index=False)
            csv_content = csv_buffer.getvalue()

            smtp_server = "smtp.gmail.com" if "gmail" in server_type else "smtp.office365.com"
            
            msg = MIMEMultipart()
            msg['From'] = user
            msg['To'] = user 
            msg['Subject'] = f"📊 NeuroMail Auto-Save: {len(buffer_data)} New Emails"
            msg.attach(MIMEText(f"Attached is the analysis for the last {len(buffer_data)} emails.", 'plain'))

            part = MIMEBase('application', "octet-stream")
            part.set_payload(csv_content)
            encoders.encode_base64(part)
            timestamp = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M")
            part.add_header('Content-Disposition', f'attachment; filename="neuromail_backup_{timestamp}.csv"')
            msg.attach(part)

            server_conn = smtplib.SMTP(smtp_server, 587)
            server_conn.starttls()
            server_conn.login(user, password)
            server_conn.sendmail(user, user, msg.as_string())
            server_conn.quit()
            return True
        except Exception as e:
            print(f"Autosave Error: {e}")
            return False

    def _loop(self, model, server, user, password, backlog_limit, autosave_limit):
        label_map = {0: "Low", 1: "Medium", 2: "High"}
        
        while self.is_running:
            try:
                # Force Port 993 for Render/Cloud
                mail = imaplib.IMAP4_SSL(server, 993)
                mail.login(user, password)
                mail.select("inbox")
                
                _, messages = mail.search(None, 'UNSEEN')
                raw_ids = messages[0].split()
                
                if raw_ids:
                    email_ids = sorted([int(x) for x in raw_ids], reverse=True)
                    ids_to_process = []

                    # High Water Mark Logic
                    if self.max_id_seen == 0:
                        ids_to_process = email_ids[:backlog_limit]
                        if ids_to_process: 
                            self.max_id_seen = max(ids_to_process)
                            self.status = f"Initial Scan: {len(ids_to_process)} emails..."
                    else:
                        ids_to_process = [x for x in email_ids if x > self.max_id_seen]
                        if ids_to_process: 
                            self.max_id_seen = max(ids_to_process)
                            self.status = f"New Mail: Processing {len(ids_to_process)}..."
                        else:
                            self.status = f"Monitoring... (Up to date. Last ID: {self.max_id_seen})"

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
                                        
                                        new_df = pd.DataFrame([new_row])
                                        self.data = pd.concat([new_df, self.data], ignore_index=True)
                                        self.buffer.append(new_row)
                                        self.last_update = datetime.datetime.now().strftime("%H:%M:%S")

                                        if len(self.buffer) >= autosave_limit:
                                            self.status = f"Sending Auto-Save ({len(self.buffer)} emails)..."
                                            if self._send_autosave_email(self.buffer, user, password, server):
                                                self.buffer = []
                                                self.status = "Auto-Save Sent. Resuming..."
                            except: continue
                else:
                    self.status = "Inbox Empty (No Unread)"

                mail.logout()
                
                for _ in range(15):
                    if not self.is_running: break
                    time.sleep(1)
                    
            except Exception as e:
                self.status = f"Error: {e}"
                time.sleep(30)

# --- 4. HELPER FUNCTIONS ---
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

# --- 5. SIDEBAR ---
scanner = BackgroundScanner() # Initialize Global Scanner

with st.sidebar:
    st.title("🧠 NeuroMail Pro")
    
    # AUTO LOAD MODEL
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

    st.markdown("---")
    backlog_limit = st.number_input("Initial Backlog Scan", value=50)
    autosave_limit = st.number_input("Auto-Save Every (X) Emails", value=50)

    c1, c2 = st.columns(2)
    if c1.button("🟢 START"):
        if email_user and email_pass:
            scanner.start(model, imap_server, email_user, email_pass, backlog_limit, autosave_limit)
            st.rerun()
    if c2.button("🔴 STOP"):
        scanner.stop()
        st.rerun()
    
    if st.button("Clear Dashboard"):
        scanner.data = pd.DataFrame()
        st.rerun()

# --- 6. DASHBOARD ---
st.markdown(f"### Status: {'🟢 Running' if scanner.is_running else '🔴 Stopped'}")
st.markdown(f"<div class='status-box'>{scanner.status} | Last Update: {scanner.last_update}</div>", unsafe_allow_html=True)

# Auto-refresh ONLY if running (saves resources)
if scanner.is_running:
    time.sleep(5)
    st.rerun()

if not scanner.data.empty:
    df = scanner.data
    
    sort_map = {"High": 0, "Medium": 1, "Low": 2, "Unknown": 3}
    df['SortKey'] = df['Priority'].map(sort_map)
    df = df.sort_values(by=['Time', 'SortKey'], ascending=[False, True]).drop('SortKey', axis=1)
    
    c1, c2, c3 = st.columns(3)
    c1.metric("High", len(df[df['Priority']=="High"]))
    c2.metric("Medium", len(df[df['Priority']=="Medium"]))
    c3.metric("Low", len(df[df['Priority']=="Low"]))
    
    st.dataframe(df, use_container_width=True, hide_index=True)
