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

# --- 1. THE GHOST BOT (Background Thread) ---
@st.cache_resource
class BackgroundScanner:
    def __init__(self):
        self.data = pd.DataFrame() # The Dashboard Data (Never clears automatically)
        self.buffer = []           # The Autosave Buffer (Clears after emailing)
        self.is_running = False
        self.last_update = "System Start"
        self.seen_emails = set()
        self.max_id_seen = 0       # The High Water Mark
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
        """Sends the CSV report to the user via email."""
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
            
            body = f"Attached is the analysis for the last {len(buffer_data)} emails scanned."
            msg.attach(MIMEText(body, 'plain'))

            part = MIMEBase('application', "octet-stream")
            part.set_payload(csv_content)
            encoders.encode_base64(part)
            timestamp = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M")
            part.add_header('Content-Disposition', f'attachment; filename="neuromail_autosave_{timestamp}.csv"')
            msg.attach(part)

            server_conn = smtplib.SMTP(smtp_server, 587)
            server_conn.starttls()
            server_conn.login(user, password)
            server_conn.sendmail(user, user, msg.as_string())
            server_conn.quit()
            return True
        except Exception as e:
            print(f"Autosave Error: {e}") # Print to logs but don't crash
            return False

    def _loop(self, model, server, user, password, backlog_limit, autosave_limit):
        label_map = {0: "Low", 1: "Medium", 2: "High"}
        
        while self.is_running:
            try:
                # Port 993 for Cloud
                mail = imaplib.IMAP4_SSL(server, 993)
                mail.login(user, password)
                mail.select("inbox")
                
                _, messages = mail.search(None, 'UNSEEN')
                raw_ids = messages[0].split()
                
                if raw_ids:
                    # Convert to Ints and Sort: Newest (Highest #) First
                    email_ids = sorted([int(x) for x in raw_ids], reverse=True)
                    
                    ids_to_process = []

                    # --- HIGH WATER MARK LOGIC ---
                    if self.max_id_seen == 0:
                        # FIRST RUN: Apply Backlog Limit
                        # If limit is 50, we take the Top 50.
                        # The rest (51+) are ignored forever.
                        ids_to_process = email_ids[:backlog_limit]
                        
                        # Set the watermark to the highest ID we grabbed.
                        if ids_to_process: 
                            self.max_id_seen = max(ids_to_process)
                            self.status = f"Initial Scan: Processing newest {len(ids_to_process)} emails..."
                    else:
                        # SUBSEQUENT RUNS: Only take IDs NEWER than the watermark
                        # This ignores all the old junk we skipped in step 1
                        ids_to_process = [x for x in email_ids if x > self.max_id_seen]
                        
                        if ids_to_process:
                            self.max_id_seen = max(ids_to_process)
                            self.status = f"New Mail: Processing {len(ids_to_process)} incoming emails..."
                        else:
                            self.status = f"Monitoring... (Up to date. Last ID: {self.max_id_seen})"

                    # --- PROCESSING LOOP ---
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
                                        
                                        # 3x Amp
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
                                        
                                        # 1. Update Dashboard (Accumulate forever until manual clear)
                                        new_df = pd.DataFrame([new_row])
                                        self.data = pd.concat([new_df, self.data], ignore_index=True)
                                        self.last_update = datetime.datetime.now().strftime("%H:%M:%S")
                                        
                                        # 2. Update Buffer (For Autosave)
                                        self.buffer.append(new_row)

                                        # --- AUTOSAVE CHECK ---
                                        if len(self.buffer) >= autosave_limit:
                                            self.status = f"Sending Auto-Save Email ({len(self.buffer)} items)..."
                                            success = self._send_autosave_email(self.buffer, user, password, server)
                                            if success:
                                                # ONLY clear buffer, NOT the dashboard data
                                                self.buffer = [] 
                                                self.status = "Auto-Save Sent. Resuming..."
                                                
                            except: continue
                else:
                    self.status = "Inbox Empty (No Unread)"

                mail.logout()
                
                # Sleep 15s
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
    st.title("🧠 NeuroMail Pro")
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

    st.markdown("---")
    # Two distinct controls
    backlog_limit = st.number_input("Initial Backlog Scan", value=50, help="How many old unread emails to verify on start.")
    autosave_limit = st.number_input("Auto-Save Every (X) Emails", value=50, help="Send a CSV backup after this many new emails.")

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
        # We DO NOT reset max_id_seen here, to prevent re-scanning old stuff
        st.rerun()

# --- 4. DASHBOARD ---
st.markdown(f"### Status: {'🟢 Running' if scanner.is_running else '🔴 Stopped'}")
st.markdown(f"<div class='status-box'>{scanner.status} | Last Update: {scanner.last_update}</div>", unsafe_allow_html=True)

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
