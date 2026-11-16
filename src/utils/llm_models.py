"""
src/utils/llm_models.py
-----------------------
Repository centralizzato di tutti i modelli LLM ed embedding utilizzati
dagli agenti e dai servizi di LTDB.
"""

# =============================================================================
# 1. Data Extraction Agent
# =============================================================================

DATA_EXTRACTION_CLEANUP_MODEL = "gemini-3.1-flash-lite"
DATA_EXTRACTION_JSON_MODEL = "gemini-3.1-flash-lite"

# =============================================================================
# 2. Article Blueprints Agent
# =============================================================================

BLUEPRINTS_WEB_SEARCH_MODEL = "gemini-3-flash-preview"
BLUEPRINTS_ENRICH_MODEL = "gemini-3.1-flash-lite"
BLUEPRINTS_DB_MATCH_MODEL = "gemini-3.1-flash-lite"
BLUEPRINTS_VALIDATE_CLUSTERS_MODEL = "gemini-3.1-flash-lite"
BLUEPRINTS_SYNTHESIZE_MODEL = "gemini-3.1-flash-lite"
BLUEPRINTS_EQUIVALENCE_ORACLE_MODEL = "gemini-3.1-flash-lite"

# =============================================================================
# 3. Codes Deduction Agent (fallback se non configurato in gemini.yaml)
# =============================================================================

CODES_DEDUCTION_DEFAULT_MODEL = "gemini-3.1-flash-lite"

# =============================================================================
# 4. Photo Search Agent
# =============================================================================

PHOTO_SEARCH_QUERY_GEN_MODEL = "gemini-3.1-flash-lite"
PHOTO_SEARCH_GROUNDED_MODEL = "gemini-3-flash-preview"

# =============================================================================
# 5. Warehouse Search Agent
# =============================================================================

WAREHOUSE_QUERY_PLAN_MODEL = "gemini-3.1-flash-lite"
WAREHOUSE_COLOR_FILTER_MODEL = "gemini-3.1-flash-lite"

# =============================================================================
# 6. Sales Chat Agent
# =============================================================================

SALES_CHAT_QUERY_PLAN_MODEL = "gemini-3.1-flash-lite"
SALES_CHAT_COLOR_FILTER_MODEL = "gemini-3.1-flash-lite"
SALES_CHAT_ANSWER_SYNTHESIS_MODEL = "gemini-3.1-flash-lite"

# =============================================================================
# 7. Servizi
# =============================================================================

BLUEPRINT_REVISION_EXPLANATION_MODEL = "gemini-3.1-flash-lite"

# =============================================================================
# 8. Embedding
# =============================================================================

DEFAULT_EMBEDDING_MODEL = "gemini-embedding-001"
