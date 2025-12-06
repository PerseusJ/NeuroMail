import streamlit as st
import email
from email.header import decode_header
import joblib
import pandas as pd
import re
import time
import datetime
import os
import hashlib
import base64
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
import streamlit.components.v1 as components

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
if 'current_user' not in st.session_state: st.session_state.current_user = None
if 'google_creds' not in st.session_state: st.session_state.google_creds = None
if 'oauth_state' not in st.session_state: st.session_state.oauth_state = None
if 'auth_url' not in st.session_state: st.session_state.auth_url = None
if 'seen_message_ids' not in st.session_state: st.session_state.seen_message_ids = set()
if 'auth_error' not in st.session_state: st.session_state.auth_error = None

# --- 4. HELPER FUNCTIONS ---
def get_client_config():
    client_id = os.environ.get("GOOGLE_CLIENT_ID")
    client_secret = os.environ.get("GOOGLE_CLIENT_SECRET")
    redirect_uri = os.environ.get("GOOGLE_REDIRECT_URI", "http://localhost:8501/")
    
    if not client_id or not client_secret:
        return None, redirect_uri
    
    config = {
        "web": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [redirect_uri]
        }
    }
    return config, redirect_uri

def build_flow(state=None):
    config, redirect_uri = get_client_config()
    if not config:
        return None, redirect_uri
    
    scopes = [
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/userinfo.email",
        "openid"
    ]
    
    flow = Flow.from_client_config(config, scopes=scopes, redirect_uri=redirect_uri)
    if state:
        flow.state = state
    return flow, redirect_uri

def creds_to_dict(creds):
    return {
        "token": creds.token,
        "refresh_token": creds.refresh_token,
        "token_uri": creds.token_uri,
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        "scopes": creds.scopes
    }

def hydrate_creds():
    cdict = st.session_state.google_creds
    if not cdict:
        return None
    try:
        creds = Credentials(
            token=cdict.get("token"),
            refresh_token=cdict.get("refresh_token"),
            token_uri=cdict.get("token_uri"),
            client_id=cdict.get("client_id"),
            client_secret=cdict.get("client_secret"),
            scopes=cdict.get("scopes"),
        )
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
            st.session_state.google_creds = creds_to_dict(creds)
        return creds
    except Exception as e:
        st.session_state.auth_error = f"Auth error: {e}"
        return None

def handle_oauth_callback():
    query_params = st.experimental_get_query_params()
    if 'code' in query_params and 'state' in query_params:
        code = query_params.get('code', [None])[0]
        state = query_params.get('state', [None])[0]
        try:
            flow, _ = build_flow(state=state)
            if not flow:
                st.session_state.auth_error = "Missing Google OAuth client config."
            elif st.session_state.oauth_state and state != st.session_state.oauth_state:
                st.session_state.auth_error = "OAuth state mismatch. Please try again."
            else:
                flow.fetch_token(code=code)
                creds = flow.credentials
                st.session_state.google_creds = creds_to_dict(creds)
                st.session_state.auth_error = None
        except Exception as e:
            st.session_state.auth_error = f"OAuth callback failed: {e}"
        # Clear query params to keep the URL clean
        st.experimental_set_query_params()

def ensure_auth_url():
    if st.session_state.auth_url:
        return st.session_state.auth_url
    
    flow, _ = build_flow()
    if not flow:
        return None
    
    auth_url, state = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent"
    )
    st.session_state.oauth_state = state
    st.session_state.auth_url = auth_url
    return auth_url

def get_gmail_service():
    creds = hydrate_creds()
    if not creds:
        return None
    try:
        return build("gmail", "v1", credentials=creds, cache_discovery=False)
    except Exception as e:
        st.session_state.auth_error = f"Gmail service error: {e}"
        return None

def logout_user():
    st.session_state.google_creds = None
    st.session_state.auth_url = None
    st.session_state.oauth_state = None
    st.session_state.monitoring = False
    st.session_state.current_user = None
    st.session_state.data = pd.DataFrame()
    st.session_state.seen_emails = set()
    st.session_state.seen_message_ids = set()

def sync_user_profile(service):
    try:
        profile = service.users().getProfile(userId='me').execute()
        email_addr = profile.get("emailAddress")
    except Exception:
        email_addr = None
    
    if not email_addr:
        return
    
    if email_addr != st.session_state.current_user:
        # User switched -> reset data
        st.session_state.current_user = email_addr
        st.session_state.data = pd.DataFrame()
        st.session_state.seen_emails = set()
        st.session_state.seen_message_ids = set()
        
        user_file = get_user_history_file(email_addr)
        if user_file and os.path.exists(user_file):
            try:
                st.session_state.data = pd.read_csv(user_file)
                for _, row in st.session_state.data.iterrows():
                    sig = f"{row.get('Sender')}_{row.get('Subject')}_{str(row.get('Content'))[:20]}"
                    st.session_state.seen_emails.add(sig)
                    if row.get('ID'):
                        st.session_state.seen_message_ids.add(str(row.get('ID')))
            except:
                pass
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
def run_scan_cycle(model, service, user_email, limit, placeholder_metrics, placeholder_table, placeholder_status, placeholder_detail):
    if not service:
        st.session_state.scan_status = "Not authenticated"
        return
    
    try:
        st.session_state.scan_status = f"Scanning latest {limit} emails"
        placeholder_status.info(f"Scanning latest {limit} emails...")
        
        resp = service.users().messages().list(userId="me", maxResults=limit).execute()
        ids_to_process = [m.get("id") for m in resp.get("messages", [])] if resp else []
        
        if not ids_to_process:
            st.session_state.scan_status = "Inbox Empty"
            return
        
        new_rows = []
        
        for msg_id in ids_to_process:
            if msg_id in st.session_state.seen_message_ids:
                continue
            
            try:
                msg_data = service.users().messages().get(userId="me", id=msg_id, format="raw").execute()
                raw_email = base64.urlsafe_b64decode(msg_data.get("raw", ""))
                msg = email.message_from_bytes(raw_email)
                row = process_single_email(msg, model, msg_id)
                
                if row == "DUPLICATE":
                    st.session_state.seen_message_ids.add(msg_id)
                    continue
                
                if row:
                    new_rows.append(row)
                    st.session_state.seen_message_ids.add(msg_id)
                    
                    temp_df = pd.DataFrame([row])
                    st.session_state.data = pd.concat([temp_df, st.session_state.data], ignore_index=True)
                    
                    sort_map = {"High": 0, "Medium": 1, "Low": 2, "Unknown": 3}
                    st.session_state.data['SortKey'] = st.session_state.data['Priority'].map(sort_map).fillna(3)
                    st.session_state.data = st.session_state.data.sort_values(by=['SortKey', 'Time'], ascending=[True, False]).drop('SortKey', axis=1)
                    
                    save_history(user_email)
                    
                    with placeholder_metrics.container():
                        render_metrics()
                    with placeholder_table.container():
                        render_table_with_selection()
                        
            except Exception as e:
                print(f"Error processing Gmail message {msg_id}: {e}")
                continue
        
        st.session_state.last_scan_time = datetime.datetime.now()
        
        if new_rows:
            st.toast(f"Found {len(new_rows)} new emails!", icon="📩")
        else:
            st.session_state.scan_status = "Monitoring (Up to date)"
            placeholder_status.markdown(f'<div class="live-badge"><div class="dot"></div>LIVE: Monitoring...</div>', unsafe_allow_html=True)
        
    except Exception as e:
        st.error(f"Gmail Error: {e}")
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
    handle_oauth_callback()
    
    service = get_gmail_service()
    if service:
        sync_user_profile(service)
    
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

        st.markdown("### 🔐 Google Login")
        client_config, redirect_uri = get_client_config()
        if not client_config:
            st.error("Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET to enable login.")
            st.caption(f"Redirect URI (default): {redirect_uri}")
        else:
            if st.session_state.auth_error:
                st.error(st.session_state.auth_error)
            if not st.session_state.google_creds:
                auth_url = ensure_auth_url()
                if auth_url:
                    st.markdown(f'<a href="{auth_url}" target="_self" style="display:block;padding:10px 14px;text-align:center;background:#0f172a;border:1px solid #334155;border-radius:10px;color:#f8fafc;text-decoration:none;font-weight:700;">Continue with Google</a>', unsafe_allow_html=True)
                    st.caption(f"Redirect URI: {redirect_uri}")
                else:
                    st.warning("Unable to generate Google login link. Check credentials.")
            else:
                logged_email = st.session_state.current_user or "Google user"
                st.success(f"Logged in as {logged_email}", icon="✅")
                if st.button("Logout", use_container_width=True):
                    logout_user()
                    st.experimental_set_query_params()
                    st.rerun()

        st.markdown("---")
        scan_limit = st.slider("Messages to scan (newest first)", 10, 500, 50)
        
        col1, col2 = st.columns(2)
        with col1:
            if st.button("🔴 Stop", use_container_width=True):
                st.session_state.monitoring = False
                st.rerun()
        with col2:
            start_btn = st.button(
                "🟢 Start",
                use_container_width=True,
                disabled=not (st.session_state.google_creds and (uploaded_file or os.path.exists(model_path)))
            )
            if start_btn:
                if not uploaded_file and not os.path.exists(model_path):
                    st.error("Model required!")
                elif not st.session_state.google_creds:
                    st.error("Google login required!")
                else:
                    st.session_state.monitoring = True
                    st.rerun()

        st.markdown("---")
        
        # Clear History (User Scoped)
        if st.button("🗑️ Clear History", use_container_width=True):
            st.session_state.data = pd.DataFrame()
            st.session_state.seen_emails = set()
            st.session_state.seen_message_ids = set()
            
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
        st.info("Login with Google to begin scanning your Gmail inbox.")

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
    if st.session_state.monitoring and (uploaded_file or os.path.exists(model_path)):
        try:
            if uploaded_file and not isinstance(uploaded_file, str):
                model = joblib.load(uploaded_file)
            else:
                model = joblib.load(model_path)
            
            if not service:
                st.session_state.monitoring = False
                st.warning("Login required to scan. Please sign in with Google.")
                return
            
            run_scan_cycle(
                model, service, st.session_state.current_user, 
                scan_limit, metrics_placeholder, table_placeholder, status_placeholder, detail_placeholder
            )
            
            time.sleep(5)
            st.rerun()
            
        except Exception as e:
            st.error(f"Runtime Error: {e}")
            st.session_state.monitoring = False

if __name__ == "__main__":
    main()
