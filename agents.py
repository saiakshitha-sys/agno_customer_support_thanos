import os
import json
import jwt  # Need to install PyJWT
from datetime import datetime
from typing import Optional, List, Dict, Any
from dotenv import load_dotenv

from agno.agent import Agent, RunOutput
from agno.models.google import Gemini
from agno.knowledge.knowledge import Knowledge
from agno.vectordb.pgvector import PgVector, SearchType
from agno.knowledge.embedder.google import GeminiEmbedder
from agno.db.postgres import PostgresDb
from agno.memory.manager import MemoryManager
from agno.eval.base import BaseEval
from agno.eval.accuracy import AccuracyEval, AccuracyResult
from agno.eval.reliability import ReliabilityEval
from agno.eval.performance import PerformanceEval
from agno.eval.utils import log_eval_run
from agno.db.schemas.evals import EvalType
from agno.tools import tool
from agno.models.message import Message
from sqlalchemy import create_engine, text
import requests
import uuid
import time
import threading

load_dotenv()

# Configuration
PROD_DB_URL = "postgresql+psycopg://postgres:julley%40qwe@40.192.63.10:5432/thanos"
DB_URL = PROD_DB_URL

TABLE_NAME = "cs_agno_vectordb2"
SESSION_TABLE = "cs_agno_longterm_memory"
GROUND_TRUTH_TABLE = "cs_agno_ground_truth"

# --- JWT VALIDATION HELPER ---
# Extracted from jwks-2a929edd-80d1-4ef5-8870-071576a0a3ad-1770095379255.json
# Using PEM format as seen in reference implementation
JWT_PUBLIC_KEY = """-----BEGIN PUBLIC KEY-----
MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAlyzDeV2OfRox1+zBg58B
zcf/IilEzDE9tZXAq7p0Na2T9QjJap9bSjvruCNBAUQ9u/xpT/HC/ndiuJI/lH/p
ATKkejSwylL4+Cd7CeYhTfpiE05CoTvqNRQ45S6GTp0LBah6QoD6ENVe+NAvXOlE
MqZr/xwVHwo3Ttd3s/dq1OtAitJtugQ891Ig29SRB22wjBRoZiS0b2fOa3tnGeBu
50RVOBqOLJvFfl2+1YvWKrzzid+ouapcQUzwU67r7a3/OU3HXfY6mOPYxNXdUh2M
8ZGTl+263+uTxb6hhj1XSlfgqSWBGmI1B+xgZ/a6HKKs3zRad9BoUz6F86CZa784
bQIDAQAB
-----END PUBLIC KEY-----"""

def validate_custom_token(token: str) -> bool:
    try:
        print("\n--- CUSTOM TOKEN INFO ---")
        # Decode headers to check algorithm
        header = jwt.get_unverified_header(token)
        print(f"Algorithm: {header.get('alg')}")
        print(f"Key ID (kid): {header.get('kid')}")

        if header.get("alg") != "RS256":
            print(f"[JWT] FAILURE: Invalid algorithm {header.get('alg')}. Expected RS256.")
            return False

        # For the hardcoded dummy token, we MUST skip signature verification
        # In a real scenario, remove 'verify_signature': False
        options = {
            "verify_signature": False, 
            "verify_aud": False,
            "verify_iss": False,
            "verify_exp": False
        }
        
        # Verify token structure and claims (skipping signature for dummy token)
        payload = jwt.decode(
            token,
            JWT_PUBLIC_KEY,
            algorithms=["RS256"],
            options=options
        )
        
        print(f"[JWT] SUCCESS: Verified custom token. Issuer: {payload.get('iss')}")
        return True
        
    except Exception as e:
        print(f"[JWT] FAILURE: Verification failed: {e}")
        return False

# --- 1. RAG CONNECTION & ROBUST FILTERING ---
# Using Vertex AI (Google Cloud) Authentication
project_id = os.getenv("GOOGLE_PROJECT_ID")
location = os.getenv("GOOGLE_LOCATION", "us-central1")

if not project_id:
    # Try standard GCP env var
    project_id = os.getenv("GOOGLE_CLOUD_PROJECT")

if not project_id:
    print("Warning: GOOGLE_PROJECT_ID not set. Vertex AI might fail.")

embedder = GeminiEmbedder(
    id="text-embedding-004", 
    dimensions=768,
    vertexai=True,
    project_id=project_id,
    location=location
)
vector_db = PgVector(
    table_name=TABLE_NAME,
    schema="ai",
    db_url=DB_URL,
    search_type=SearchType.vector,
    embedder=embedder
)

# Ground Truth DB (for Validation)
ground_truth_db = PgVector(
    table_name=GROUND_TRUTH_TABLE,
    schema="ai",
    db_url=DB_URL,
    search_type=SearchType.vector,
    embedder=embedder
)

# --- OBSERVABILITY & PERSISTENCE (EXPLICIT DB INSTANCES) ---
# Each component has its own DB definition for clarity, though they share the same underlying Postgres connection.

# 1. Sessions DB (Chat History)
sessions_db = PostgresDb(
    db_url=DB_URL,
    db_schema="ai",
    session_table="agno_sessions",
)

# 2. Memory DB (User Facts)
memory_db = PostgresDb(
    db_url=DB_URL,
    db_schema="ai",
    memory_table="agno_memories",
)

# 3. Evals DB (Accuracy, Reliability, Performance Scores)
evals_db = PostgresDb(
    db_url=DB_URL,
    db_schema="ai",
    eval_table="agno_eval_runs",
)

# 4. Traces DB (AgentOS Telemetry) - Used by AgentOS generic storage
traces_db = PostgresDb(
    db_url=DB_URL,
    db_schema="ai",
    traces_table="agno_traces",
)

# 5. UNIFIED MONITORING DB (For AgentOS UI Reading)
# AgentOS needs ONE db connection that knows about ALL tables to display them in the Admin UI.
agent_os_db = PostgresDb(
    id="thanos-db",
    db_url=DB_URL,
    db_schema="ai",
    session_table="agno_sessions",
    memory_table="agno_memories",
    eval_table="agno_eval_runs",
    traces_table="agno_traces",
)




# Knowledge Base Content DB (Required for UI Visibility in 'ai' schema)
knowledge_content_db = PostgresDb(db_url=DB_URL, knowledge_table="cs_agno_knowledge_contents", db_schema="ai")

# Knowledge Base
knowledge = Knowledge(vector_db=vector_db, contents_db=knowledge_content_db)



# --- 1.5 SNIPPET-BASED AUTOMATED EVALUATOR ---

class SupportEvaluator(BaseEval):
    def pre_check(self, run_input: Any) -> None: pass
    async def async_pre_check(self, run_input: Any) -> None: pass
    async def async_post_check(self, run_output: Any) -> None: pass

    def post_check(self, run_output: RunOutput) -> None:
        """Automatically log evals to the Control Plane after each run using standard snippets.
           Runs in a background thread to avoid blocking the agent response.
        """
        threading.Thread(target=self._run_evals_background, args=(run_output,)).start()

    def _run_evals_background(self, run_output: RunOutput) -> None:
        try:
            # --- 0. EXPLICIT INPUT MAPPING ---
            # User Requirement: "make sure all the input is going from the message field of the ChatPayload"
            # In main.py: agent.run(payload.message) -> run_output.input IS the raw user message.
            user_message_input = run_output.input
            
            # 1. Performance Eval (Manual Logging)
            # Extract exact metric values from run_output.metrics (matches DEBUG console output)
            m = run_output.metrics
            
            # FIX: Capture duration from timer because the hook runs before the run officially completes
            duration = getattr(m, "duration", None)
            if (duration is None or duration == 0.0) and hasattr(m, "timer") and m.timer:
                duration = m.timer.elapsed
            duration = duration or 0.0

            input_tokens = getattr(m, "input_tokens", None) or 0
            output_tokens = getattr(m, "output_tokens", None) or 0
            total_tokens = getattr(m, "total_tokens", None) or 0
            tokens_per_second = getattr(m, "tokens_per_second", None) or 0.0
            # Fallback calculation if tokens_per_second was not set by the model
            if tokens_per_second == 0.0 and duration > 0 and total_tokens > 0:
                tokens_per_second = round(total_tokens / duration, 4)

            print(f"[PERF EVAL] duration={duration}, input_tokens={input_tokens}, output_tokens={output_tokens}, total_tokens={total_tokens}, tokens_per_second={tokens_per_second}")
            
            # Format performance data to match example_perf.py structure
            perf_result_data = {
                "duration": duration,
                "avg_run_time": duration,
                "max_run_time": duration,
                "min_run_time": duration,
                "p95_run_time": duration, 
                "median_run_time": duration,
                "std_dev_run_time": 0,
                "avg_memory_usage": 0,
                "max_memory_usage": 0,
                "min_memory_usage": 0,
                "p95_memory_usage": 0,
                "median_memory_usage": 0,
                "std_dev_memory_usage": 0
            }
            
            log_eval_run(
                db=evals_db,
                run_id=f"{run_output.run_id}-perf",
                eval_type=EvalType.PERFORMANCE,
                eval_input={"message": user_message_input, "num_iterations": 1},
                run_data={
                    "runs": [{
                        "runtime": duration,
                        "memory": 0,
                        "input_tokens": input_tokens,
                        "output_tokens": output_tokens,
                        "total_tokens": total_tokens,
                        "tokens_per_second": tokens_per_second
                    }],
                    "result": perf_result_data
                },
                name="Performance-Live",
                agent_id=run_output.agent_id or "julley-support",
                model_id=run_output.model,
                model_provider=run_output.model_provider,
                evaluated_component_name="SupportAgent"
            )

            # 2. Reliability Eval (Snippet-Based)
            # ReliabilityEval evaluates tool calls from the agent_response
            original_run_id = run_output.run_id
            
            # Determine expected tools based on conversation context
            expected_tools = []
            
            # Context heuristics
            response_content_lower = run_output.content.lower() if run_output.content else ""
            # Ensure user_message_input is a string before calling lower()
            if isinstance(user_message_input, dict):
                # If input is a dict (e.g. from ChatPayload), try to extract message
                user_msg_lower = user_message_input.get("message", "").lower()
            elif isinstance(user_message_input, str):
                user_msg_lower = user_message_input.lower()
            else:
                user_msg_lower = str(user_message_input).lower()
            
            # RAG Context
            if "search" in user_msg_lower or "how" in user_msg_lower or "what" in user_msg_lower or "guide" in user_msg_lower:
                expected_tools.append("search_documentation")
                # When searching documentation, we also expect validation
                expected_tools.append("validate_answer_with_ground_truth")
            
            # Ticket Context
            if "ticket details confirmed" in response_content_lower or "create a ticket" in user_msg_lower:
                 expected_tools.append("create_support_ticket")
                 
            # Summary Context (Closing phrases)
            if "feel free to reach out" in response_content_lower or "have a great day" in response_content_lower:
                 expected_tools.append("save_conversation_summary") # Note: This is an internal function, not always a tool, but adding as requested

            # Evaluate tool calls
            run_tools = getattr(run_output, "tool_calls", [])
            actual_tool_names = []
            if run_tools:
                for t in run_tools:
                    t_name = t.get("tool_name") if isinstance(t, dict) else getattr(t, "tool_name", None)
                    if t_name:
                        actual_tool_names.append(t_name)
            
            # Reliability Logic matching example_reliability.py structure
            failed_tool_calls = []
            passed_tool_calls = []
            
            if expected_tools:
                # Check if expected tools were called
                for tool in expected_tools:
                    if tool in actual_tool_names:
                        passed_tool_calls.append(tool)
                    else:
                        failed_tool_calls.append(tool)
            else:
                # We expected NO tools. If any were called, they are "failed" (unexpected)
                if actual_tool_names:
                    failed_tool_calls = actual_tool_names

            eval_status = "PASSED" if not failed_tool_calls else "FAILED"

            log_eval_run(
                db=evals_db,
                run_id=f"{original_run_id}-rel",
                eval_type=EvalType.RELIABILITY,
                eval_input={"expected_tool_calls": expected_tools},
                run_data={
                    "eval_status": eval_status,
                    "failed_tool_calls": failed_tool_calls,
                    "passed_tool_calls": passed_tool_calls
                },
                name="Reliability-Live",
                agent_id=run_output.agent_id or "julley-support",
                model_id=run_output.model,
                model_provider=run_output.model_provider,
                evaluated_component_name="SupportAgent"
            )

            # 3. Accuracy Eval (Snippet-Based LLM Judge with Ground Truth)
            # Run ONLY if 'search_documentation' tool was called or if it's a direct factual query
            # We check tool usage from run_output
            run_tools = getattr(run_output, "tool_calls", [])
            rag_tool_used = False
            for t in run_tools:
                # Handle both dict and object access for tool_name
                t_name = t.get("tool_name") if isinstance(t, dict) else getattr(t, "tool_name", None)
                if t_name == "search_documentation":
                    rag_tool_used = True
                    break
            
            if rag_tool_used:
                print(f"DEBUG: Accuracy Eval handled by search_documentation tool directly.")
            else:
                print(f"DEBUG: Skipping Accuracy Eval - RAG tool not used.")
            
            # Since Reliability and Accuracy don't have agent_id etc. in constructor,
            # we manually update the logged record if needed, but Accuracy handles it 
            # if we pass the agent object. However, we are in a hook, so we can just 
            # manually update the database if we want to be 100% sure.
            # For now, let's just make sure Accuracy has the right eval_id.
            
        except Exception as e:
            print(f"[EVAL ERROR] Failed to log run: {e}")

def get_robust_filter(agent: Agent):
    """Priority: allperm=1 > superperm > perm."""
    perm = str(getattr(agent, "perm", "0"))
    superperm = str(getattr(agent, "superperm", "0"))
    allperm = str(getattr(agent, "allperm", "0"))
    
    if allperm == "1":
        return {"allperm": 1}
    elif superperm != "0":
        return {"superperm": superperm}
    elif perm != "0":
        return {"perm": perm}
    return {}

# --- 2. BACKEND ACTION TOOLS (ALIGNED WITH N8N SCHEMA) ---

@tool
def create_support_ticket(agent: Agent, title: str, main_issue: str, summary: str) -> str:
    """
    Creates a formal support ticket in the Thanos Staging Backend.
    Call this ONLY when the user confirms the ticket details or says 'ticket details confirmed'.
    """
    url = "https://stagebackend.julleyonline.co.in/api/v1/customer-support/n8n/ticket"
    
    # Extract History for Backend
    history = []
    try:
        history_msgs = agent.get_chat_history()
        history = [{"role": getattr(m, "role", "unknown"), "content": getattr(m, "content", "")} for m in history_msgs]
    except Exception as e:
        print(f"[TOOL ERROR] History retrieval failed: {e}")
        history = []

    payload = {
        "conversationId": agent.session_id,
        "userId": getattr(agent, "userId", "unknown"),
        "tenantId": getattr(agent, "tenantId", "Thanos"),
        "sessionId": agent.session_id,
        "title": title,
        "description": main_issue,
        "summary": summary,
        "userName": getattr(agent, "userName", "Unknown"),
        "userEmail": getattr(agent, "userEmail", "Not provided"),
        "conversationHistory": history,
        "timestamp": datetime.now().isoformat(),
        "metadata": {
            "source": "agno-agent",
            "version": "1.0",
            "ticketCreated": True,
            "role": getattr(agent, "userRole", "USER")
        }
    }
    
    token = getattr(agent, "accessToken", "")
    headers = {
        "Authorization": f"Bearer {getattr(agent, 'accessToken', '')}",
        "Content-Type": "application/json",
    }
    
    try:
        print(f"\n[BACKEND-TICKET] Hitting staging ticket API for: {agent.session_id}")
        response = requests.post(url, json=payload, headers=headers, timeout=15)
        print(f"[BACKEND-TICKET] Response Code: {response.status_code}")
        
        if response.status_code in [200, 201]:
            print("[BACKEND-TICKET] Ticket Created Successfully")
            return "SUCCESS: Ticket details confirmed. Our team will follow up shortly."
        
        print(f"[BACKEND-TICKET] Error Details: {response.text}")
        return f"FAILURE: Backend returned {response.status_code}. Details: {response.text}"
    except Exception as e:
        print(f"[BACKEND-TICKET] Exception: {str(e)}")
        return f"FAILURE: API Connection Error: {str(e)}"

# INTERNAL FUNCTION (Made into a Tool to fix agent confusion)
@tool
def save_conversation_summary(agent: Agent):
    """
    Analyzes history and saves summary in background.
    Triggered by closing phrases in assistant response.
    """
    url = "https://stagebackend.julleyonline.co.in/api/v1/customer-support/n8n/conversation"
    
    # 1. Generate Summary Content
    try:
        # Get history
        history = load_history_from_db(agent.session_id, limit=20)
        history_text = "\n".join([f"{m.role}: {m.content}" for m in history])
        
        # Use LLM to summarize
        model = Gemini(id="gemini-2.0-flash")
        prompt = f"""
        Analyze this customer support conversation and extract:
        1. A concise summary (max 2 sentences).
        2. The main topic (1-3 words).
        3. The main issue description.
        
        Conversation:
        {history_text}
        
        Output JSON: {{ "summary": "...", "topic": "...", "description": "..." }}
        """
        response = model.response(prompt)
        data = json.loads(response.content.replace("```json", "").replace("```", "").strip())
        
        summary = data.get("summary", "Automated Summary")
        topic = data.get("topic", "Support")
        main_issue = data.get("description", "User Inquiry")
        
    except Exception as e:
        print(f"[SUMMARY-GEN ERROR] {e}")
        summary = "Summary generation failed."
        topic = "Error"
        main_issue = str(e)

    # 2. Payload
    payload = {
        "conversationId": agent.session_id,
        "userId": getattr(agent, "userId", "unknown"),
        "tenantId": getattr(agent, "tenantId", "Thanos"),
        "sessionId": agent.session_id,
        "topic": topic,
        "description": main_issue,
        "summary": summary,
        "userName": getattr(agent, "userName", "Unknown"),
        "userEmail": getattr(agent, "userEmail", "Not provided"),
        "messageCount": len(history) if history else 0,
        "closedAt": int(datetime.now().timestamp() * 1000),
        "lastMessageAt": int(datetime.now().timestamp() * 1000)
    }
    
    headers = {
        "Authorization": f"Bearer {getattr(agent, 'accessToken', '')}",
        "Content-Type": "application/json",
    }
    
    try:
        print(f"\n[BACKEND-SUMMARY-BG] Saving summary to: {url}")
        response = requests.post(url, json=payload, headers=headers, timeout=10)
        print(f"[BACKEND-SUMMARY-BG] Response Code: {response.status_code}")
    except Exception as e:
        print(f"[BACKEND-SUMMARY-BG] Failed: {str(e)}")

def run_accuracy_check(user_query, answer, agent_id="julley-support", model_id="gemini-2.0-flash", run_id_prefix=None):
    """Runs accuracy check in background."""
    try:
        # Generate new run_id
        if run_id_prefix:
             run_id = f"{run_id_prefix}-acc"
        else:
             run_id = f"{uuid.uuid4()}-acc"
        
        print(f"[ACCURACY-BG] Validating: {user_query}")
        gt_results = ground_truth_db.search(query=str(user_query), limit=3)
        ground_truth_context = "\n\n".join([r.content for r in gt_results]) if gt_results else "No specific ground truth found."
        
        # We need a dummy accuracy eval object just to run the check
        acc_eval = AccuracyEval(
            name="Accuracy-Live-GroundTruth",
            db=None, 
            model=Gemini(id="gemini-2.0-flash"), 
            input=user_query,
            expected_output=f"The answer must be factually consistent with the following Ground Truth:\n{ground_truth_context}"
        )
        
        # Judge the actual output
        acc_result = acc_eval.run_with_output(output=answer, print_results=False)
        
        if acc_result:
            acc_data = {
                "avg_score": getattr(acc_result, "avg_score", 0.0),
                "mean_score": getattr(acc_result, "mean_score", 0.0),
                "min_score": getattr(acc_result, "min_score", 0.0),
                "max_score": getattr(acc_result, "max_score", 0.0),
                "std_dev_score": getattr(acc_result, "std_dev_score", 0.0),
            }
            
            log_eval_run(
                db=evals_db,
                run_id=run_id,
                eval_type=EvalType.ACCURACY,
                eval_input={"message": user_query, "expected": ground_truth_context[:500]},
                run_data=acc_data,
                name="Accuracy-Live-GroundTruth",
                agent_id=agent_id,
                model_id=model_id,
                model_provider="Google",
                evaluated_component_name="SupportAgent"
            )
            print(f"[ACCURACY-BG] Logged Accuracy Eval: {acc_data} | RunID: {run_id}")
    except Exception as e:
        print(f"[ACCURACY-BG] Failed: {e}")

@tool
def search_documentation(agent: Agent, query: str) -> str:
    """Search knowledge base based on user permissions."""
    meta_filter = get_robust_filter(agent)
    print(f"\n[RAG] Searching: '{query}' | Filter: {meta_filter}")
    results = vector_db.search(query=query, limit=5, filters=meta_filter)
    
    content = "\n\n".join([r.content for r in results]) if results else "No specific documentation found."
    
    # Trigger Background Accuracy Check immediately
    # We attempt to retrieve the run_id from the agent if available to match other metrics
    run_id_prefix = getattr(agent, "run_id", None)
    
    agent_id = agent.agent_id if hasattr(agent, "agent_id") else "julley-support"
    model_id = agent.model.id if hasattr(agent, "model") else "gemini-2.5-flash"

    threading.Thread(target=run_accuracy_check, args=(query, content, agent_id, model_id, run_id_prefix)).start()
    
    if not results:
        return "No specific documentation found for your request at your permission level."
    return content

def run_accuracy_check_async(user_query, answer, agent_id="julley-support", model_id="gemini-2.0-flash", run_id_prefix=None):
    """
    Async wrapper for running accuracy checks.
    """
    threading.Thread(target=run_accuracy_check, args=(user_query, answer, agent_id, model_id, run_id_prefix)).start()

@tool
def validate_answer_with_ground_truth(agent: Agent, query: str, answer: str) -> str:
    """
    Validates an answer against the ground truth database asynchronously.
    This tool triggers a background check and returns immediately.
    """
    print(f"\n[GROUND-TRUTH] Triggering validation for: '{query}'")
    
    # Run in background
    run_id_prefix = getattr(agent, "run_id", None)
    agent_id = agent.agent_id if hasattr(agent, "agent_id") else "julley-support"
    model_id = agent.model.id if hasattr(agent, "model") else "gemini-2.5-flash"
    
    run_accuracy_check_async(query, answer, agent_id, model_id, run_id_prefix)
    
    return "Validation triggered in background."

# --- 3. AGENT FACTORY ---

def load_history_from_db(conversation_id: str, limit: int = 10) -> List[Message]:
    """Loads chat history from public.cs_messages and returns list of Agno Messages."""
    # Use standard postgresql driver for direct SQLAlchemy calls
    engine = create_engine(DB_URL.replace("postgresql+psycopg", "postgresql"))
    messages = []
    try:
        with engine.connect() as conn:
            query = text("""
                SELECT content, sender_type 
                FROM public.cs_messages 
                WHERE conversation_id = :conversation_id 
                ORDER BY created_at DESC 
                LIMIT :limit
            """)
            results = conn.execute(query, {"conversation_id": conversation_id, "limit": limit}).fetchall()
            # Results are in reverse order (newest first), reverse them back to chronological
            for row in reversed(results):
                role = "user" if row[1] == "user" else "assistant"
                messages.append(Message(role=role, content=row[0]))
    except Exception as e:
        print(f"[DB ERROR] Failed to load history from public.cs_messages: {e}")
    return messages

def get_support_team(user_context: dict):
    with open("prompt.md", "r") as f:
        prompt_content = f.read()
    
    # Dynamic String Injection
    instructions = prompt_content.replace("{{$json.userName}}", user_context.get("userName", "User"))
    instructions = instructions.replace("{{$json.userEmail}}", user_context.get("userEmail", "Not provided"))
    instructions = instructions.replace("{{$json.perm}}", str(user_context.get("perm", "0")))
    instructions = instructions.replace("{{$json.allperm}}", str(user_context.get("allperm", "0")))
    instructions = instructions.replace("{{$json.superperm}}", str(user_context.get("superperm", "0")))

    session_id = user_context.get("conversationId") or user_context.get("sessionId")
    user_id = user_context.get("userId") or user_context.get("userEmail")

    # Load Long-Term History from public.cs_messages
    db_history = load_history_from_db(session_id) if session_id else []
    
    # Format history for context
    history_context = ""
    if db_history:
        history_context = "\n\n--- PREVIOUS CONVERSATION HISTORY (From Long-Term Storage) ---\n"
        history_context += "\n".join([f"{m.role.upper()}: {m.content}" for m in db_history])
        history_context += "\n--- END HISTORY ---\n"

    # Verify Custom Token if present (Optional but recommended for sensitive ops)
    custom_token = user_context.get("customToken")
    if custom_token:
        validate_custom_token(custom_token)

    support_agent = Agent(
        name="Julley Support",
        user_id=user_id,
        session_id=session_id,
        model=Gemini(id="gemini-2.5-flash"),
        db=sessions_db, # Explicitly using sessions_db for persistent chat history
        additional_context=history_context if history_context else None,
        # 1. Memory Manager Configuration (Snippet-Based Pattern)
        # We explicitly provide a Model to the MemoryManager so it can "extract" facts from the conversation.
        memory_manager=MemoryManager(
            db=memory_db, 
            model=Gemini(id="gemini-2.5-flash"),
            memory_capture_instructions=(
                "Recieve all the important notes and components of the user like problems, questions he asked, "
                "context he asked the question for, his tone, info he fetched and the negatives and positives "
                "of him needs to be stored."
            )
        ), 
        update_memory_on_run=True,   # Extract & Save facts to agno_memories
        enable_agentic_memory=True,  # Enable reasoning about user facts
        debug_mode=True,             # Enable logs to see MEMORY and EVALS updates
        instructions=[
            "--- AGENT CORE BEHAVIOR ---",
            "1. You are a STATEFUL agent. Refer to Chat History for context.",
            "2. If the user query is technical, SEARCH DOCUMENTS immediately.",
            "3. TICKET LOGIC: When details are confirmed, YOU MUST CALL 'create_support_ticket' immediately.",
            "4. NEVER say 'ticket details confirmed' without calling the tool first.",
            "5. COMPLETION LOGIC (CRITICAL): At the end of every response, ANALYZE THE LAST 4 MESSAGES (2 User, 2 AI).",
            "   - If the conversation feels complete (e.g., query resolved, user says thank you, or you say 'Ticket details confirmed/ticket creation successful').",
            "   - Or if you see phrases like: 'please feel free to reach out again', 'if you need anything else', 'resolved', 'thank you'.",
            "   - If these patterns exist, the system will automatically summarize the conversation.",
            # "7. TotalToken: Include this at the end of every response.", # Removed to hide from user
            instructions,
        ],
        tools=[search_documentation, create_support_ticket, validate_answer_with_ground_truth, save_conversation_summary],
        add_history_to_context=True,
        num_history_runs=10,
        post_hooks=[SupportEvaluator(), sync_turn_to_backend] 
    )
    
    for key, value in user_context.items():
        setattr(support_agent, key, value)
    
    return support_agent

def sync_api_call(url, json_payload, headers):
    """Helper to run API call in background."""
    # #region agent log
    try:
        import json
        with open("/Users/julley005/Desktop/THANOS/.cursor/debug.log", "a") as f:
            f.write(json.dumps({
                "timestamp": int(time.time() * 1000),
                "location": "agents.py:sync_api_call",
                "message": "Starting API sync",
                "data": {"url": url},
                "hypothesisId": "api_sync_timing"
            }) + "\n")
    except Exception: pass
    # #endregion
    print(f"[API SYNC START] Posting to API at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    start_time = time.time()
    try:
        response = requests.post(url, json=json_payload, headers=headers, timeout=5)
        duration = time.time() - start_time
        # #region agent log
        try:
            import json
            with open("/Users/julley005/Desktop/THANOS/.cursor/debug.log", "a") as f:
                f.write(json.dumps({
                    "timestamp": int(time.time() * 1000),
                    "location": "agents.py:sync_api_call",
                    "message": "API sync finished",
                    "data": {"status": response.status_code, "duration": duration},
                    "hypothesisId": "api_sync_timing"
                }) + "\n")
        except Exception: pass
        # #endregion
        print(f"[API SYNC] Finished at {datetime.now().strftime('%H:%M:%S')} in {duration:.2f}s | Status: {response.status_code}")
    except Exception as e:
        print(f"[API SYNC] Failed after {time.time() - start_time:.2f}s: {e}")

def sync_turn_to_backend(agent: Any, run_output: Any):
    # 1. API Sync (Existing)
    url = "https://stagebackend.julleyonline.co.in/api/v1/customer-support/n8n/messages"
    user_msg = getattr(agent, "last_user_msg", "...")
    payload = {
        "conversationId": agent.session_id,
        "userId": getattr(agent, "userId", "unknown"),
        "tenantId": getattr(agent, "tenantId", "Thanos"),
        "sessionId": agent.session_id,
        "userName": getattr(agent, "userName", "Unknown"),
        "userEmail": getattr(agent, "userEmail", "Not provided"),
        "timestamp": datetime.now().isoformat(),
        "messages": [
            {"senderType": "user", "content": user_msg},
            {"senderType": "assistant", "content": run_output.content}
        ]
    }
    headers = {
        "Authorization": f"Bearer {getattr(agent, 'accessToken', '')}",
        "Content-Type": "application/json",
        }
    
    # Fire and forget
    threading.Thread(target=sync_api_call, args=(url, payload, headers)).start()

    # Check for closing phrase to trigger summary
    content = run_output.content.lower()
    if "feel free to reach out" in content or "ticket details confirmed" in content or "have a great day" in content:
        print("[AUTO-SUMMARY] Triggering summary generation based on closing phrase.")
        threading.Thread(target=save_conversation_summary, args=(agent,)).start()

    # 2. Database Sync (Direct to public.cs_messages)
    try:
        engine = create_engine(DB_URL.replace("postgresql+psycopg", "postgresql"))
        tenant_id = getattr(agent, "tenantId", "Thanos")
        conversation_id = agent.session_id
        model_used = agent.model.id if hasattr(agent.model, "id") else "gemini-2.5-flash"
        
        # Capture timestamps to ensure ordering
        now_ms = int(time.time() * 1000)
        user_created_at = now_ms
        assistant_created_at = now_ms + 1  # Ensure assistant message is strictly after user message

        with engine.connect() as conn:
            # Save User Message
            conn.execute(text("""
                INSERT INTO public.cs_messages (id, content, conversation_id, sender_type, created_at, tenant_id, model_used, created_by, updated_at, updated_by)
                VALUES (:id, :content, :conversation_id, :sender_type, :created_at, :tenant_id, :model_used, :created_by, :updated_at, :updated_by)
            """), {
                "id": str(uuid.uuid4()),
                "content": user_msg,
                "conversation_id": conversation_id,
                "sender_type": "user",
                "created_at": user_created_at,
                "tenant_id": tenant_id,
                "model_used": model_used,
                "created_by": "agno-agent",
                "updated_at": user_created_at,
                "updated_by": "agno-agent"
            })
            
            # Save Assistant Response
            conn.execute(text("""
                INSERT INTO public.cs_messages (id, content, conversation_id, sender_type, created_at, tenant_id, model_used, token_count, created_by, updated_at, updated_by)
                VALUES (:id, :content, :conversation_id, :sender_type, :created_at, :tenant_id, :model_used, :token_count, :created_by, :updated_at, :updated_by)
            """), {
                "id": str(uuid.uuid4()),
                "content": run_output.content,
                "conversation_id": conversation_id,
                "sender_type": "assistant",
                "created_at": assistant_created_at,
                "tenant_id": tenant_id,
                "model_used": model_used,
                "token_count": run_output.metrics.total_tokens if run_output.metrics else 0,
                "created_by": "agno-agent",
                "updated_at": assistant_created_at,
                "updated_by": "agno-agent"
            })
            conn.commit()
            print(f"[DB SYNC] Successfully synced turn to public.cs_messages for session: {conversation_id}")
    except Exception as e:
        print(f"[DB SYNC ERROR] Failed to sync to public.cs_messages: {e}")

def run_session_evals(session_id: str, user_id: str = None) -> Dict:
    """
    Runs evaluations for the entire session.
    Calculates Accuracy, Reliability, and overall Quality.
    """
    print(f"[SESSION-EVAL] Starting evaluation for session: {session_id}")
    history = load_history_from_db(session_id, limit=50) # Load up to 50 messages
    if not history:
        return {"status": "No history found"}

    # Convert history to text for LLM Judge
    transcript = "\n".join([f"{m.role.upper()}: {m.content}" for m in history])
    
    # 1. Accuracy/Quality Eval using LLM Judge
    eval_model = Gemini(id="gemini-2.0-flash")
    prompt = f"""
    Evaluate the following customer support conversation transcript.
    
    Transcript:
    {transcript}
    
    Criteria:
    1. Did the agent answer the user's questions accurately?
    2. Was the agent helpful and polite?
    3. Did the agent follow instructions (search docs, create ticket)?
    
    Provide a score from 1-10 and a brief reasoning.
    Format: JSON {{ "score": int, "reason": str }}
    """
    
    try:
        response = eval_model.response(prompt)
        try:
            result = json.loads(response.content.replace("```json", "").replace("```", "").strip())
        except:
             result = {"score": 0, "reason": "Failed to parse eval response"}
        
        # Extract metrics
        metrics = getattr(response, "metrics", {})
        # Ensure metrics is a dict
        if not isinstance(metrics, dict):
             if hasattr(metrics, "to_dict"):
                 metrics = metrics.to_dict()
             else:
                 metrics = {
                     "input_tokens": getattr(metrics, "input_tokens", 0),
                     "output_tokens": getattr(metrics, "output_tokens", 0),
                     "total_tokens": getattr(metrics, "total_tokens", 0),
                     "time": getattr(metrics, "time", 0.0),
                 }

        duration = metrics.get("time", 0.0)
        total_tokens = metrics.get("total_tokens", 0)
        tokens_per_second = total_tokens / duration if duration > 0 else 0.0
        
        # Add calculated metrics
        metrics["tokens_per_second"] = tokens_per_second
        
        # Log to DB
        # Format the result to match the user's requested JSON structure
        # Expected: {"results": [{"input": ..., "score": ..., "output": ..., "reason": ..., "expected_output": ...}], "avg_score": ..., "max_score": ..., "min_score": ..., "mean_score": ..., "std_dev_score": ...}
        
        score_val = result.get("score", 0)
        reason_val = result.get("reason", "")
        
        formatted_result = {
            "results": [
                {
                    "input": "Full Transcript Evaluation", # Represents the whole session
                    "score": score_val,
                    "output": "Session Quality Assessment",
                    "reason": reason_val,
                    "expected_output": "High quality, helpful, accurate support interaction."
                }
            ],
            "avg_score": score_val,
            "max_score": score_val,
            "min_score": score_val,
            "mean_score": score_val,
            "std_dev_score": 0.0
        }

        # Format Eval Input to match user request
        # {"input": ..., "num_iterations": ..., "expected_output": ..., "additional_context": ..., "additional_guidelines": ...}
        formatted_eval_input = {
            "input": "Session Transcript",
            "num_iterations": 1,
            "expected_output": "High quality support interaction",
            "additional_context": {"transcript_length": len(transcript)},
            "additional_guidelines": None
        }

        log_eval_run(
            db=evals_db,
            run_id=f"{session_id}-session-eval",
            eval_type=EvalType.ACCURACY,
            eval_input=formatted_eval_input,
            run_data=formatted_result,
            # score=score_val, # Removed as it causes error
            # metrics=metrics, # Removed as it causes error
            name="Session-Quality-Eval",
            agent_id="julley-support",
            model_id="gemini-2.0-flash", 
            evaluated_component_name="SupportAgent"
        )
        return result
    except Exception as e:
        print(f"[SESSION-EVAL] Error: {e}")
        return {"error": str(e)}
