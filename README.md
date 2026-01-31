# Agno AgentOS for Thanos Customer Support

This project implements a **Role-Based Multi-Agent RAG System** using the [Agno Framework](https://github.com/agno-agi/agno). It replaces legacy n8n workflows with a stateful, Python-based agentic service that integrates directly with the Thanos Backend.

## 🚀 System Architecture

1.  **Knowledge Base (RAG)**:
    *   **Source**: Google Drive (PDF Manuals for Pilots, Technicians, Admins).
    *   **Storage**: Postgres `pgvector` (`cs_agno_vectordb1`) with role-based metadata (`perm`, `superperm`).
    *   **Ingestion**: `upsert_drive_docs.py` syncs Drive content to the vector DB.

2.  **Agent Logic (`agents.py`)**:
    *   **Model**: Gemini 2.0 Flash (`gemini-2.0-flash-exp`).
    *   **Context**: Loads the last 10 messages from `cs_agno_longterm_memory`.
    *   **Tools**:
        *   `search_documentation`: Hybrid search with permission filtering.
        *   `create_support_ticket`: Hits Thanos Backend to create Jira/Support tickets.
        *   `save_conversation_summary`: Summarizes resolved chats to the backend.

3.  **API Server (`main.py`)**:
    *   Exposes a FastAPI endpoint (`/chat`) to receive user messages from Thanos Backend.
    *   Handles role validation and dynamic prompt injection.

---

## 🛠️ Setup & Installation

### 1. Prerequisites
*   Python 3.10+
*   Postgres with `pgvector` extension installed.
*   Google Cloud Service Account (`credentials.json`) with Drive Read permissions.

### 2. Environment Variables
Create a `.env` file based on `.env.example`:

```bash
cp .env.example .env
```

**Required Variables:**
*   `DATABASE_URL`: Postgres connection string (e.g., `postgresql+psycopg://user:pass@host:5432/db`).
*   `GOOGLE_API_KEY`: API Key for Gemini Models.
*   `GOOGLE_APPLICATION_CREDENTIALS`: Path to your service account JSON.
*   `GOOGLE_DRIVE_FOLDER_ID`: ID of the Drive folder containing manuals.

### 3. Installation

```bash
pip install -r requirements.txt
```

---

## 🏃‍♂️ Usage

### Phase 1: Ingest Knowledge Base
Run the sync script to download files from Drive and upsert them into the vector database.

```bash
python upsert_drive_docs.py
```
*This will create the table `cs_agno_vectordb1` if it doesn't exist and populate it.*

### Phase 2: Run the Agent Server
Start the FastAPI server to handle chat requests.

```bash
python main.py
```
*Server runs on `http://0.0.0.0:8000`.*

---

## 🔗 API Integration

The server exposes a single endpoint for the frontend/backend bridge:

**POST** `/api/v1/chat`

**Payload:**
```json
{
  "message": "How do I reset my drone?",
  "conversationId": "uuid-123",
  "sessionId": "session-xyz",
  "userName": "Pilot John",
  "userRole": "PILOT",
  "tenantId": "Thanos"
}
```

**Response:**
```json
{
  "response": "To reset your drone, please follow these steps from the Pilot Manual...",
  "history": [...]
}
```

---

## 📂 Project Structure

*   `agents.py`: core agent definitions, tools, and memory logic.
*   `upsert_drive_docs.py`: ETL script for Google Drive -> PgVector.
*   `main.py`: FastAPI application entry point.
*   `prompt.md`: System prompt template with dynamic variable injection.
*   `production_rag_plan.md`: Detailed architectural roadmap and status.
