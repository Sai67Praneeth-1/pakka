# # ============================================================
# ⚖️ Eudia Legal Summarizer Backend (Firebase + Gemini)
# ============================================================
import os
import re, datetime
import json

from typing import List
from fastapi import FastAPI, UploadFile, File, Form, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from firebase_admin import credentials, initialize_app, firestore, storage as fb_storage, auth
import google.generativeai as genai
from PyPDF2 import PdfReader
import tempfile

# ============================================================
# 1️⃣ LOCAL CONFIGURATION (PATHS & KEYS)
# ============================================================
cred_path = r"C:\Users\kavya\Downloads\firebase-key.json"
bucket_name = "eudia-legalsummarizer.appspot.com"
gemini_key = "AIzaSyA_a7h3lQDZAGoy0IQrWUkKrTIiRCjThDg"

# ============================================================
# 2️⃣ FIREBASE INITIALIZATION (FIRESTORE + STORAGE)
# ============================================================
cred = credentials.Certificate(cred_path)
initialize_app(cred, {"storageBucket": bucket_name})

# Firestore database
db = firestore.client()

# Firebase Storage (no ADC needed)
bucket = fb_storage.bucket()

# ============================================================
# 3️⃣ GEMINI CONFIGURATION (WITH ROBUST MODEL SELECTION + FALLBACK)
# ============================================================
genai.configure(api_key=gemini_key)

def get_gemini_model():
    """
    Finds and returns a supported Gemini model, starting with a preferred list.
    """
    preferred_models = ["gemini-2.5-flash", "gemini-1.5-flash", "gemini-1.0-pro"]
    
    for model_name in preferred_models:
        try:
            model_info = genai.get_model(f'models/{model_name}')
            if 'generateContent' in model_info.supported_generation_methods:
                print(f"✅ Using preferred model: {model_name}")
                return genai.GenerativeModel(model_name)
        except Exception:
            continue # Try the next model in the preferred list

    # Fallback if no preferred models are available
    for m in genai.list_models():
        if 'generateContent' in m.supported_generation_methods:
            print(f"⚠️ Preferred models not found. Using fallback: {m.name}")
            return genai.GenerativeModel(m.name)
            
    raise RuntimeError("❌ No supported Gemini model found. Please check your API key and model availability.")

model = get_gemini_model()

# ============================================================
# 4️⃣ FASTAPI INITIALIZATION
# ============================================================
app = FastAPI(title="Eudia Legal Summarizer API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # in production, specify your frontend URL
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ============================================================
# 5️⃣ FIREBASE AUTHENTICATION UTILITY
# ============================================================
def verify_firebase_token(id_token: str):
    """Verifies a Firebase ID token and returns the user's data."""
    try:
        decoded_token = auth.verify_id_token(id_token)
        return decoded_token
    except Exception as e:
        # Token is invalid, expired, or revoked
        return None

# ============================================================
# 5️⃣ PDF TEXT EXTRACTION
# ============================================================
def extract_text_from_pdf(pdf_path: str) -> str:
    """Extracts text content from a PDF file."""
    text = ""
    try:
        reader = PdfReader(pdf_path)
        for page in reader.pages:
            content = page.extract_text()
            if content:
                text += content + "\n"
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"PDF extraction failed: {e}")
    return text.strip()

# ============================================================
# 6️⃣ SUMMARIZE USING GEMINI
# ============================================================
def summarize_with_gemini(text: str) -> str:
    """Summarizes a legal document using Gemini."""
    try:
        response = model.generate_content(
            f"Summarize this legal document clearly and concisely. "
            f"Focus on case facts, sections, judgment, and conclusion:\n\n{text}"
        )
        return response.text
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Gemini summarization failed: {e}")

# ============================================================
# 6.5️⃣ EXTRACT STRUCTURED SUMMARY USING GEMINI
# ============================================================
def extract_structured_summary(text: str) -> dict:
    """
    Uses Gemini to extract key legal elements from the case text.
    Returns a structured JSON object.
    """
    prompt = f"""
    You are a legal document parser. Extract the following structured details
    from the case text below. If a field is not present, return null.

    Fields to extract (in JSON):
    {{
      "Case_Name": "",
      "Parties": {{
        "Petitioner": "",
        "Respondent": ""
      }},
      "Court_Name": "",
      "Judge": "",
      "Sections_Invoked": [],
      "Facts": "",
      "Judgment": "",
      "Final_Order": "",
      "Date_of_Judgment": ""
    }}

    Case Text:
    {text}
    """

    try:
        response = model.generate_content(prompt)
        # Ensure we get JSON even if Gemini adds text
        match = re.search(r'\{.*\}', response.text, re.DOTALL)
        if match:
            return json.loads(match.group(0))
        else:
            return {"error": "No structured JSON found"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Gemini extraction failed: {e}")

# ============================================================
# 7️⃣ EXTRACT FUTURE HEARING DATES
# ============================================================
def extract_future_dates(text: str):
    """
    Finds all dates in various formats and returns only those after today.
    """
    # Comprehensive regex to find multiple date formats
    date_pattern = (
        r'\b(?:\d{1,2}\s*(?:st|nd|rd|th)?\s*(?:January|February|March|April|May|June|July|August|September|October|November|December)[\s,]*\d{4}'
        r'|(?:January|February|March|April|May|June|July|August|September|October|November|December)\s*\d{1,2},?\s*\d{4}'
        r'|(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{4}'
        r'|\d{4}-\d{2}-\d{2}'
        r'|\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4})\b'
    )

    matches = re.finditer(date_pattern, text, flags=re.IGNORECASE)
    dates_found = []
    current_date = datetime.datetime.now()

    for match in matches:
        date_str = match.group(0).strip()
        parsed_date = None

        # Define formats to try, from most specific to least specific
        formats_to_try = [
            "%d %B %Y", "%B %d, %Y", "%B %Y", 
            "%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y",
            "%d/%m/%y", "%d-%m-%y", "%d.%m.%y"
        ]

        cleaned_date_str = re.sub(r'(\d+)(st|nd|rd|th)', r'\1', date_str)

        for fmt in formats_to_try:
            try:
                parsed_date = datetime.datetime.strptime(cleaned_date_str.replace(",", ""), fmt)
                break 
            except ValueError:
                continue

        if parsed_date and parsed_date > current_date:
            dates_found.append(parsed_date.strftime("%d %B %Y"))

    unique_dates = sorted(list(set(dates_found)))
    return unique_dates, len(unique_dates)

# ============================================================
# 🔍 EXTRACT CASE NUMBERS
# ============================================================
def extract_case_numbers(text: str):
    """
    Extracts case numbers such as:
    - W.P.(C) 123 of 2021
    - Criminal Appeal No. 45/2022
    - Case No. 234-2020
    Returns list of unique case numbers found and their count.
    """
    pattern = r'\b(?:[A-Z.\(\) ]+)?No\.?\s*\d+[\/\-]?\d{4}\b'
    matches = re.findall(pattern, text, flags=re.IGNORECASE)
    case_numbers = list(set([m.strip() for m in matches if m.strip()]))
    return case_numbers, len(case_numbers)

# ============================================================
# 🗓️ GENERATE CLEAN CASE TIMELINE
# ============================================================
def generate_case_timeline(text: str) -> str:
    """
    Extracts all timeline dates and their nearest clear event descriptions.
    Returns a Markdown-formatted table with clean, readable events.
    """

    import re, datetime
    from datetime import datetime as dt

    # Date formats: "21 October 2025", "October 21, 2025", "March 2026", etc.
    date_pattern = (
        r'\b(?:\d{1,2}\s*(?:January|February|March|April|May|June|July|August|September|October|November|December)[,]?\s*\d{4}'
        r'|(?:January|February|March|April|May|June|July|August|September|October|November|December)\s*\d{1,2},?\s*\d{4}'
        r'|(?:January|February|March|April|May|June|July|August|September|October|November|December)\s*\d{4}'
        r'|\d{4}-\d{2}-\d{2})\b'
    )

    # Split text into sentences to get clean event context
    sentences = re.split(r'(?<=[.!?])\s+', text)

    events = []
    current_date = datetime.datetime.now()

    for sentence in sentences:
        match = re.search(date_pattern, sentence, flags=re.IGNORECASE)
        if not match:
            continue
        date_str = match.group(0).strip()

        # Try to parse date
        parsed_date = None
        for fmt in ["%d %B %Y", "%B %d %Y", "%B %Y", "%Y-%m-%d"]:
            try:
                parsed_date = dt.strptime(date_str.replace(",", ""), fmt)
                break
            except Exception:
                continue

        # Handle month-year only (e.g., March 2026)
        if not parsed_date:
            month_match = re.match(r"(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{4})", date_str, re.IGNORECASE)
            if month_match:
                month_name, year = month_match.groups()
                parsed_date = dt.strptime(f"15 {month_name} {year}", "%d %B %Y")

        if not parsed_date:
            continue

        # Clean up the event sentence
        event_text = re.sub(date_pattern, '', sentence).strip()
        event_text = re.sub(r'\s+', ' ', event_text)
        event_text = event_text.replace(' - ', '; ').strip()
        if len(event_text) < 5:
            continue  # skip fragments

        status = "✅ Completed" if parsed_date < current_date else "⏳ Upcoming"
        events.append((parsed_date, date_str, event_text, status))

    if not events:
        return "No timeline events detected."

    # Sort chronologically
    events.sort(key=lambda x: x[0])

    # Build Markdown table
    table = "| **Date** | **Event** | **Status** |\n"
    table += "|-----------|------------|-------------|\n"
    for _, date_str, event, status in events:
        short_event = event[:120] + ("..." if len(event) > 120 else "")
        table += f"| {date_str} | {short_event} | {status} |\n"

    return table

# ============================================================
# 7️⃣ UPLOAD + SUMMARIZE + STORE IN FIREBASE
# ============================================================
@app.post("/upload/")
async def upload_and_summarize(
    files: List[UploadFile] = File(...),
    client_name: str = Form(...),
    file_type: str = Form(...),
    authorization: str = Header(None)
):
    # 🔐 Verify Firebase Auth
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")
    
    id_token = authorization.split("Bearer ")[1]
    user = verify_firebase_token(id_token)
    if not user:
        raise HTTPException(status_code=403, detail="Invalid or expired Firebase token")
    
    user_id = user["uid"]
    summaries = []

    for file in files:
        temp_path = None
        try:
            # 1️⃣ Save temp file
            with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as temp:
                temp.write(await file.read())
                temp_path = temp.name

            # 2️⃣ Extract text
            text = extract_text_from_pdf(temp_path)
            if not text:
                # Skip file if no text could be extracted
                continue

            # 3️⃣ Summarize using Gemini
            summary = summarize_with_gemini(text)

            # 3.5️⃣ Extract structured data
            structured_data = extract_structured_summary(text)

            # 4️⃣ Extract future hearing dates
            future_dates, date_count = extract_future_dates(text)

            # Extract case numbers
            case_numbers, case_number_count = extract_case_numbers(text)

            # 5️⃣ Generate timeline
            timeline_table = generate_case_timeline(text)

            # Count sections found (from structured summary)
            sections_found_count = len(structured_data.get("Sections_Invoked", [])) if structured_data else 0

            # Build metadata block
            metadata = {
                "upcoming_dates": {
                    "count": date_count
                },
                "sections": {
                    "count": sections_found_count
                },
                "case_numbers": {
                    "count": case_number_count,
                    "list": case_numbers
                }
            }

            # Store summary + metadata in Firestore
            doc_ref = db.collection("summaries").document()
            doc_ref.set({
                "user_id": user_id,
                "filename": file.filename,
                "client_name": client_name,
                "file_type": file_type,
                "summary": summary,
                "structured_summary": structured_data,
                "metadata": metadata,
                "timeline": timeline_table,
                "timestamp": firestore.SERVER_TIMESTAMP
            })

            summaries.append({
                "filename": file.filename, 
                "summary": summary,
                "structured_summary": structured_data,
                "metadata": metadata,
                "timeline": timeline_table
            })

        finally:
            # 6️⃣ Clean up temp file
            if temp_path and os.path.exists(temp_path):
                os.remove(temp_path)

    # 7️⃣ Return response
    return {"summaries": summaries}


# ============================================================
# 8️⃣ HEALTH CHECK ENDPOINT
# ============================================================
@app.get("/")
def root():
    return {
        "status": "✅ Backend running",
        "project_id": "eudia-legalsummarizer",
        "firestore_database": "(default)",
        "storage_bucket": bucket_name
    }
