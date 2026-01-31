import os
import time
import io
import json
import shutil
from pathlib import Path
from dotenv import load_dotenv

from googleapiclient.discovery import build
from google.oauth2 import service_account
from googleapiclient.http import MediaIoBaseDownload

from agno.knowledge.knowledge import Knowledge
from agno.vectordb.pgvector import PgVector, SearchType
from agno.knowledge.embedder.google import GeminiEmbedder

# Load environment variables
load_dotenv()

# Configuration
DB_URL = os.getenv("DATABASE_URL")
# if DB_URL:
#     DB_URL = DB_URL.strip().strip("'").strip('"')
# else:
#     DB_URL = "postgresql://ai:ai@localhost:5432/thanos"

TABLE_NAME = "cs_agno_vectordb1"
SERVICE_ACCOUNT_FILE = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "credentials.json").strip()
FOLDER_ID = os.getenv("GOOGLE_DRIVE_FOLDER_ID")
if FOLDER_ID:
    FOLDER_ID = FOLDER_ID.strip()

# 1. Setup Vector DB & Knowledge Base
# Using Gemini Embedder for consistency with the Gemini agent team
vector_db = PgVector(
    table_name=TABLE_NAME,
    schema="ai",
    db_url=DB_URL,
    search_type=SearchType.hybrid,
    embedder=GeminiEmbedder(id="text-embedding-004", dimensions=768)
)

knowledge = Knowledge(
    vector_db=vector_db,
)
# Ensure table exists
# vector_db.create() # Usually handled by Agno when inserting if configured

# 2. Replicate n8n Permission Mapping from production_rag_plan.md
DOCUMENT_ROLE_MAPPING = {
    "1k6uBRowoVMw62PKPdvA2mB7hfnwJniyd": {"roles": ["CUSTOMER_ADMIN", "ADMIN"], "perm": 0, "superperm": "1"},
    "14l-20sHn7xh86tIDK2_pemrTGcEEpprU": {"roles": ["TECHNICIAN", "ADMIN"], "perm": "3", "superperm": 0},
    "1w4-08CCssM7dB9LSQot-sGXFwvpNbjIN": {"roles": ["PILOT", "CUSTOMER_ADMIN", "ADMIN"], "perm": "1", "superperm": "1"},
    "1kE0NGpe9-UtIpSPMg5RPWq0AIWmC4hfL": {"roles": ["SENIOR_CS", "CUSTOMER_SUPPORT", "ADMIN"], "perm": "2", "superperm": "2"},
}

def get_google_drive_service():
    """Authenticates and returns the Google Drive service."""
    if not os.path.exists(SERVICE_ACCOUNT_FILE):
        raise FileNotFoundError(f"Service account file not found at {SERVICE_ACCOUNT_FILE}")
    
    creds = service_account.Credentials.from_service_account_file(
        SERVICE_ACCOUNT_FILE, 
        scopes=['https://www.googleapis.com/auth/drive.readonly']
    )
    return build('drive', 'v3', credentials=creds)

def download_file(service, file_id, file_name, local_dir):
    """Downloads a file from Google Drive to a local directory."""
    request = service.files().get_media(fileId=file_id)
    local_path = Path(local_dir) / file_name
    
    # Ensure directory exists
    local_path.parent.mkdir(parents=True, exist_ok=True)
    
    fh = io.FileIO(local_path, 'wb')
    downloader = MediaIoBaseDownload(fh, request)
    done = False
    while done is False:
        status, done = downloader.next_chunk()
        print(f"   Downloading {file_name}: {int(status.progress() * 100)}%")
    
    return str(local_path)

def upsert_document(file_path, file_id, file_name):
    """
    Inserts a document into PgVector with specific role-based metadata.
    Using Agno 2.x knowledge.insert() for built-in reading and chunking.
    """
    mapping = DOCUMENT_ROLE_MAPPING.get(file_id, {"roles": ["ADMIN"], "perm": 0, "superperm": 0})
    
    metadata = {
        "file_id": file_id,
        "file_name": file_name,
        "perm": mapping["perm"],
        "superperm": mapping["superperm"],
        "allperm": 1 if "ADMIN" in mapping["roles"] else 0,
        "tenant_id": "Thanos",
        "source": "google_drive",
        "upserted_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    }
    
    print(f"🚀 Processing: {file_name} for Knowledge Base...")
    
    # In Agno 2.x, knowledge.insert() handles:
    # 1. Reading the file (PDF, Text, etc.)
    # 2. Chunking the content
    # 3. Generating embeddings
    # 4. Upserting into the vector database
    knowledge.insert(path=file_path, metadata=metadata)
    
    print(f"✅ Successfully processed {file_name}")

def verify_db_persistence():
    """Manual SQL check to confirm data is in the database."""
    from sqlalchemy import create_engine, text
    engine = create_engine(DB_URL.replace("postgresql+psycopg", "postgresql"))
    with engine.connect() as conn:
        count = conn.execute(text(f"SELECT COUNT(*) FROM ai.{TABLE_NAME}")).scalar()
        print(f"\n� Final DB Check: Total rows in {TABLE_NAME} = {count}")
        if count > 0:
            sample = conn.execute(text(f"SELECT name, meta_data FROM ai.{TABLE_NAME} LIMIT 1")).fetchone()
            print(f"   Sample Document: {sample[0]}")

def sync_google_drive():
    """
    Syncs Google Drive folder to PgVector.
    """
    if not FOLDER_ID:
        print("❌ Error: GOOGLE_DRIVE_FOLDER_ID not set in .env")
        return

    # Load service account info for debugging
    with open(SERVICE_ACCOUNT_FILE, 'r') as f:
        sa_info = json.load(f)
        sa_email = sa_info.get("client_email")
        print(f"🔑 Using Service Account: {sa_email}")
        print(f"👉 Ensure this email has 'Viewer' access to the folder!")

    service = get_google_drive_service()
    
    print(f"🔄 Syncing Google Drive Folder ID: {FOLDER_ID}")
    
    try:
        # Verify Folder Access
        folder_meta = service.files().get(
            fileId=FOLDER_ID, 
            fields="name, driveId", 
            supportsAllDrives=True
        ).execute()
        print(f"📁 Folder Name: {folder_meta.get('name')}")
        if folder_meta.get('driveId'):
            print(f"ℹ️ This is a Shared Drive folder (Drive ID: {folder_meta.get('driveId')})")
    except Exception as e:
        print(f"⚠️ Warning: Could not verify folder metadata. Error: {e}")
        print("Continuing anyway...")

    # Query for all files in folder
    # Added supportsAllDrives and includeItemsFromAllDrives for Shared Drive support
    query = f"'{FOLDER_ID}' in parents and trashed = false"
    print(f"🔍 Running Query: {query}")
    
    results = service.files().list(
        q=query, 
        fields="files(id, name, mimeType)",
        supportsAllDrives=True,
        includeItemsFromAllDrives=True,
        spaces='drive'
    ).execute()
    
    files = results.get('files', [])

    if not files:
        print("ℹ️ No files found in the specified folder.")
        return

    # Temporary directory for downloads
    temp_dir = "temp_drive_files"
    if os.path.exists(temp_dir):
        shutil.rmtree(temp_dir)
    os.makedirs(temp_dir)

    try:
        for file in files:
            print(f"📄 Processing: {file['name']} ({file['id']})")
            
            # Handle Google Docs/Sheets/Slides by exporting them as PDFs
            if "google-apps" in file['mimeType']:
                print(f"   Exporting Google Doc '{file['name']}' as PDF...")
                request = service.files().export_media(fileId=file['id'], mimeType='application/pdf')
                local_path = os.path.join(temp_dir, f"{file['name']}.pdf")
                with io.FileIO(local_path, 'wb') as fh:
                    downloader = MediaIoBaseDownload(fh, request)
                    done = False
                    while done is False:
                        _, done = downloader.next_chunk()
            else:
                local_path = download_file(service, file['id'], file['name'], temp_dir)
            
            # Upsert into Agno Knowledge Base
            upsert_document(local_path, file['id'], file['name'])

    finally:
        # Clean up temporary files
        if os.path.exists(temp_dir):
            shutil.rmtree(temp_dir)
            print(f"🧹 Cleaned up temporary directory: {temp_dir}")

if __name__ == "__main__":
    try:
        sync_google_drive()
        verify_db_persistence()
        print("\n✨ Phase 1 Sync Complete!")
    except Exception as e:
        print(f"\n❌ Sync failed: {str(e)}")
