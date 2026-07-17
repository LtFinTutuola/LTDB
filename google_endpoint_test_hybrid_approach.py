import time
import os
import json
import uuid
import pdfplumber
from google import genai
from google.genai import types
from typing import List, Optional
from pydantic import BaseModel, Field
import re
import yaml

# Global list to store LLM usage logs
LLM_LOGS = {
    "system_prompts": {},
    "calls": []
}

# =====================================================================
# DEFINIZIONE DELLO SCHEMA DI VALIDAZIONE RIGIDO (PYDANTIC)
# =====================================================================
class MappedFieldsSheet(BaseModel):
    category: str = Field(description="Product category. MUST be exactly one of the values provided in the prompt's Allowed Categories list.")
    sex: str = Field(description="Target gender for the product. MUST be exactly one of the values provided in the prompt's Allowed Sex list.")
    materials: List[str] = Field(description="List of materials of the product. Extract the materials directly from the provided text.")
    colors: List[str] = Field(description="List of colors of the product inferred from codes or web images.")

class FreeFormFieldsSheet(BaseModel):
    product_name: str = Field(description="Official name of the product. DO NOT include the SKU or VendorCode in this string.")
    product_short_description: str = Field(description="Concise, technical, and precise description of the product to help an ERP operator identify it. NO commercial or promotional fluff.")
    product_extended_description: str = Field(description="Extended and comprehensive description of the product, including all its details; This field is aimed to be used in downstream semantic search, so it must be comprehensive.")
    tags: List[str] = Field(description="List of up to 10 descriptive tags related to the article (e.g., color, style, specific material details, usage) to enhance semantic search.")

# Configurazione: recupera la chiave API in modo sicuro dalle variabili d'ambiente o api.yaml
API_KEY = os.environ.get("GEMINI_API_KEY")
if not API_KEY:
    try:
        with open("api.yaml", "r") as f:
            api_config = yaml.safe_load(f)
            API_KEY = api_config.get("api_key")
    except Exception:
        pass

#Inizializza il client ufficiale Google GenAI
client = genai.Client(api_key=API_KEY)

def extract_raw_text_locally(pdf_path):
    """
    Esegue il pre-processing locale del PDF estraendo tutto il testo.
    Restituisce una stringa contenente tutto il testo del documento.
    """
    print(f"Avvio pre-processing locale con pdfplumber sul file '{pdf_path}'...")
    raw_text = []
    
    try:
        with pdfplumber.open(pdf_path) as pdf:
            for i, page in enumerate(pdf.pages):
                text = page.extract_text()
                if text:
                    raw_text.append(text)
                            
        full_text = "\n--- PAGINA ---\n".join(raw_text)
        print(f"Pre-processing completato: estratto il testo di {len(raw_text)} pagine.")
        print(full_text)
        return full_text
    except Exception as e:
        print(f"Errore durante l'estrazione locale: {str(e)}")
        return None


def stage_3a_web_search(mapped_item, brand_name):
    vendor_code = mapped_item.get('VendorCode', '')
    description = mapped_item.get('Description', '')
    color = mapped_item.get('Color', '')
    
    print(f"  -> Stage 3a (Web Search) for SKU: {vendor_code}...")
    
    LLM_LOGS["system_prompts"]["stage_3a"] = (
        "You are an AI Agent specialized in Product Data Enrichment for a retail ERP system. "
        "Your goal is to browse the web, find the official technical sheet or e-commerce page "
        "of the requested product and return a comprehensive description of the product, "
        "including all its details, such as product colors, materials, dimensions and a detailed description.\n"
        "CRITICAL RULES:\n"
        "1. Use the Google search tool to find real specifications.\n"
        "2. MUST wrap specific information in exact XML-like tags:\n"
        "   <COLORS>...</COLORS>\n"
        "   <MATERIALS>...</MATERIALS>\n"
        "   <DIMENSIONS>...</DIMENSIONS>\n"
        "   <DESCRIPTION>...</DESCRIPTION>\n"
        "3. If a data point is not available online, write 'Dato non disponibile' inside its tag.\n"
        "4. IMPORTANT: Write the output in Italian. DO NOT cite textual sources or generate a <SOURCES> tag in the output."
    )
    
    config = types.GenerateContentConfig(
        tools=[{"google_search": {}}],
        response_mime_type="text/plain",
        max_output_tokens=1024,
        system_instruction=LLM_LOGS["system_prompts"]["stage_3a"]
    )

    prompt = (
        f"Find all technical and commercial specifications for this product.\n\n"
        f"--- STARTING DATA ---\n"
        f"Brand: {brand_name}\n"
        f"SKU / Model: {vendor_code}\n"
        f"Original Description: {description}\n"
        f"Color Code: {color}\n\n"
        f"--- SUGGESTED TARGET SEARCH QUERY ---\n"
        f"\"{brand_name} {vendor_code}\" OR \"{brand_name} {description}\"\n\n"
        f"Analyze the web results and return the detailed description with the required XML tags."
    )
    
    try:
        response = client.models.generate_content(
            model='gemini-3.1-flash-lite',
            contents=prompt,
            config=config
        )
        
        call_id = str(uuid.uuid4())
        input_tokens = response.usage_metadata.prompt_token_count if response.usage_metadata else 0
        output_tokens = response.usage_metadata.candidates_token_count if response.usage_metadata else 0
        
        query_usage = 0
        extracted_urls = []
        if response.candidates and response.candidates[0].grounding_metadata:
            gm = response.candidates[0].grounding_metadata
            if hasattr(gm, 'web_search_queries') and gm.web_search_queries:
                query_usage = len(gm.web_search_queries)
            if hasattr(gm, 'grounding_chunks'):
                for chunk in gm.grounding_chunks:
                    if hasattr(chunk, 'web') and chunk.web:
                        if hasattr(chunk.web, 'uri') and chunk.web.uri:
                            extracted_urls.append(chunk.web.uri)
                        elif hasattr(chunk.web, 'title') and chunk.web.title:
                            extracted_urls.append(chunk.web.title)
        
        LLM_LOGS["calls"].append({
            "call_id": call_id,
            "pipeline_stage": "Stage 3a - Web Search",
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "query_usage": query_usage,
            "original_prompt": prompt,
            "original_output": response.text
        })
        
        return {"raw_text": response.text, "call_id_3a": call_id, "urls": extracted_urls}
        
    except Exception as e:
        print(f"     [Error during Stage 3a for {vendor_code}]: {str(e)}")
        return {"raw_text": "", "call_id_3a": None, "urls": []}


def parse_stage_3a_output(raw_text):
    colors = re.search(r'<COLORS>(.*?)</COLORS>', raw_text, re.DOTALL)
    materials = re.search(r'<MATERIALS>(.*?)</MATERIALS>', raw_text, re.DOTALL)
    
    extracted_relevant_info = ""
    if colors:
        extracted_relevant_info += f"COLORS:\n{colors.group(1).strip()}\n\n"
    if materials:
        extracted_relevant_info += f"MATERIALS:\n{materials.group(1).strip()}\n\n"
        
    return extracted_relevant_info.strip()


def stage_3b_fields_mapping(parsed_text, raw_text, allowed_categories, allowed_sex):
    print(f"  -> Stage 3b (Fields Mapping)...")
    
    LLM_LOGS["system_prompts"]["stage_3b"] = (
        "You are an AI mapping assistant. Read the provided product details and map them strictly to the allowed JSON schema values.\n"
        "CRITICAL RULES:\n"
        "1. 'category' and 'sex' MUST be populated using strictly one of the values provided in the Allowed lists. Extract 'materials' directly from the text.\n"
        "2. All output data values MUST be written in Italian.\n"
        "3. You must return arrays for 'materials' and 'colors' instead of single string values."
    )
    
    config = types.GenerateContentConfig(
        response_mime_type="application/json",
        response_schema=MappedFieldsSheet,
        system_instruction=LLM_LOGS["system_prompts"]["stage_3b"]
    )

    context = parsed_text if parsed_text else raw_text
    prompt = (
        f"Map the following data to the required JSON schema.\n\n"
        f"--- PRODUCT DETAILS ---\n{context}\n\n"
        f"--- ALLOWED MAPPING VALUES ---\n"
        f"Allowed Categories: {', '.join(allowed_categories)}\n"
        f"Allowed Sex: {', '.join(allowed_sex)}\n"
    )
    
    try:
        response = client.models.generate_content(
            model='gemini-3.1-flash-lite',
            contents=prompt,
            config=config
        )
        
        call_id = str(uuid.uuid4())
        input_tokens = response.usage_metadata.prompt_token_count if response.usage_metadata else 0
        output_tokens = response.usage_metadata.candidates_token_count if response.usage_metadata else 0
        
        LLM_LOGS["calls"].append({
            "call_id": call_id,
            "pipeline_stage": "Stage 3b - Fields Mapping",
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "query_usage": 0,
            "original_prompt": prompt,
            "original_output": response.text
        })
        
        parsed = json.loads(response.text)
        parsed["call_id_3b"] = call_id
        return parsed
        
    except Exception as e:
        print(f"     [Error during Stage 3b]: {str(e)}")
        return {}


def stage_3c_free_form_completion(raw_text):
    print(f"  -> Stage 3c (Free-Form Completion)...")
    
    LLM_LOGS["system_prompts"]["stage_3c"] = (
        "You are an AI describing assistant. Read the provided raw web data and generate comprehensive, free-form fields.\n"
        "CRITICAL RULES:\n"
        "1. DO NOT include the SKU or VendorCode inside the 'product_name'.\n"
        "2. Populate 'tags' with up to 10 semantic keywords describing the item to enhance downstream search.\n"
        "3. All output data values MUST be written in Italian."
    )

    config = types.GenerateContentConfig(
        response_mime_type="application/json",
        response_schema=FreeFormFieldsSheet,
        system_instruction=LLM_LOGS["system_prompts"]["stage_3c"]
    )

    prompt = (
        f"Generate the free-form description fields and tags based on this raw data.\n\n"
        f"--- RAW WEB DATA ---\n{raw_text}\n"
    )
    
    try:
        response = client.models.generate_content(
            model='gemini-3.1-flash-lite',
            contents=prompt,
            config=config
        )
        
        call_id = str(uuid.uuid4())
        input_tokens = response.usage_metadata.prompt_token_count if response.usage_metadata else 0
        output_tokens = response.usage_metadata.candidates_token_count if response.usage_metadata else 0
        
        LLM_LOGS["calls"].append({
            "call_id": call_id,
            "pipeline_stage": "Stage 3c - Free Form Completion",
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "query_usage": 0,
            "original_prompt": prompt,
            "original_output": response.text
        })
        
        parsed = json.loads(response.text)
        parsed["call_id_3c"] = call_id
        return parsed
        
    except Exception as e:
        print(f"     [Error during Stage 3c]: {str(e)}")
        return {}



def extract_erp_data_production_ready(pdf_path):

    """
    Pipeline ibrida: 
    1. Estrae il testo localmente dal PDF.
    2. Usa Gemini (3.1 Pro o 3.5 Flash) per normalizzare la semantica.
    """
    # Fase 1: Pre-processing
    raw_text = extract_raw_text_locally(pdf_path)

    if not raw_text:
        return {"error": "Impossibile estrarre testo localmente dal PDF."}
        
    print("Testo estratto con successo dal PDF.")

    model_name = 'gemini-3.1-flash-lite'
    
    try:
        # --- FASE 1: Pulizia del testo grezzo ---
        print("Avvio pulizia del testo tramite LLM...")
        LLM_LOGS["system_prompts"]["stage_1"] = (
            "Sei un assistente specializzato nell'estrazione dati. "
            "Il tuo compito è pulire il testo grezzo di una bolla di spedizione B2B. "
            "Rimuovi intestazioni, note legali, totali, dati bancari o qualsiasi altro dato "
            "che non riguardi strettamente gli articoli/prodotti spediti. "
            "Restituisci SOLO il testo relativo alle righe degli articoli, includendo anche le intestazioni delle colonne, senza alterare i dati in esse contenuti."
        )
        cleanup_config = types.GenerateContentConfig(
            response_mime_type="text/plain",
            system_instruction=LLM_LOGS["system_prompts"]["stage_1"]
        )
        
        cleanup_prompt = f"Pulisci questo testo mantenendo solo le righe degli articoli:\n\n{raw_text}"
        
        try:
            print(f"Tentativo di pulizia con il modello: [{model_name}]...")
            cleanup_response = client.models.generate_content(
                model=model_name,
                contents=cleanup_prompt,
                config=cleanup_config
            )
            cleaned_text = cleanup_response.text
            
            call_id_1 = str(uuid.uuid4())
            input_tokens_1 = cleanup_response.usage_metadata.prompt_token_count if cleanup_response.usage_metadata else 0
            output_tokens_1 = cleanup_response.usage_metadata.candidates_token_count if cleanup_response.usage_metadata else 0
            
            LLM_LOGS["calls"].append({
                "call_id": call_id_1,
                "pipeline_stage": "Stage 1 - Raw Text Cleanup",
                "input_tokens": input_tokens_1,
                "output_tokens": output_tokens_1,
                "query_usage": 0,
                "original_prompt": cleanup_prompt,
                "original_output": cleaned_text
            })
        except Exception as error:
            return {"error": f"Errore durante la pulizia: {str(error)}"}

        print(f"Testo pulito generato:\n{cleaned_text}\n")

        # --- FASE 2: Mappatura in JSON ---
        print("Avvio mappatura JSON del testo pulito tramite LLM...")
        LLM_LOGS["system_prompts"]["stage_2"] = (
            "Sei un Agente di Document Intelligence per un sistema ERP. "
            "Riceverai in input un testo pulito contenente solo gli articoli di una bolla di spedizione B2B. "
            "Il tuo compito è analizzare semanticamente questi dati "
            "e restituire ESCLUSIVAMENTE un array JSON di oggetti normalizzati con le seguenti chiavi: "
            "VendorCode (es. modello/codice prodotto), Barcode (se presente), Description (descrizione del prodotto), Color (codice/colore), Quantity (leggendo i dati relativi alle quantità o confezioni associate all'articolo)."
        )
        mapping_config = types.GenerateContentConfig(
            response_mime_type="application/json",
            system_instruction=LLM_LOGS["system_prompts"]["stage_2"]
        )

        mapping_prompt = f"Analizza e mappa questo testo in JSON:\n\n{cleaned_text}"
        
        try:
            print(f"Tentativo di estrazione JSON con il modello: [{model_name}]...")
            mapping_response = client.models.generate_content(
                model=model_name,
                contents=mapping_prompt,
                config=mapping_config
            )
            
            call_id_2 = str(uuid.uuid4())
            input_tokens_2 = mapping_response.usage_metadata.prompt_token_count if mapping_response.usage_metadata else 0
            output_tokens_2 = mapping_response.usage_metadata.candidates_token_count if mapping_response.usage_metadata else 0
            
            LLM_LOGS["calls"].append({
                "call_id": call_id_2,
                "pipeline_stage": "Stage 2 - JSON Mapping",
                "input_tokens": input_tokens_2,
                "output_tokens": output_tokens_2,
                "query_usage": 0,
                "original_prompt": mapping_prompt,
                "original_output": mapping_response.text
            })
            
            return json.loads(mapping_response.text)
        except Exception as error:
            return {"error": f"Errore durante la mappatura JSON: {str(error)}"}

    except Exception as e:
        return {"error": f"Si è verificato un errore fatale nell'elaborazione: {str(e)}"}
    finally:
        # Opzionale ma consigliato: pulizia del file sul server per non consumare quota
        if 'uploaded_file' in locals():
             print("Pulizia: eliminazione del file dal server Google...")
             client.files.delete(name=uploaded_file.name)

# --- Esempio di Esecuzione ---
if __name__ == "__main__":
    import yaml
    from datetime import datetime
    
    with open("config.yaml", "r") as f:
        config = yaml.safe_load(f)

    whitelist = config.get("logging_files_whitelist", [])
    today_str = datetime.now().strftime("%Y%m%d")
    for folder in ["raw_data_extractions", "llm_usage_logs"]:
        if os.path.exists(folder):
            for filename in os.listdir(folder):
                file_path = os.path.join(folder, filename)
                norm_path = file_path.replace("\\", "/")
                if norm_path in whitelist:
                    continue
                file_date = datetime.fromtimestamp(os.path.getmtime(file_path)).strftime("%Y%m%d")
                if file_date != today_str:
                    try:
                        os.remove(file_path)
                        print(f"Eliminato file obsoleto: {file_path}")
                    except Exception:
                        pass

    test_pdf = config.get("input_file_path", config.get("test_file_path"))
    
    # Simula un controllo di esistenza del file prima di procedere
    if test_pdf and os.path.exists(test_pdf):
        risultato = extract_erp_data_production_ready(test_pdf)
        print("\n--- Dati Strutturati Estratti ---")
        print(json.dumps(risultato, indent=4))
        
        if isinstance(risultato, list) or "error" not in risultato:
            brand_name = config.get("input_file_brand", "Unknown Brand")
            allowed_categories = config.get("product_categories", [])
            allowed_sex = config.get("sex", [])
            
            output_dir = "raw_data_extractions"
            os.makedirs(output_dir, exist_ok=True)
            
            base_name = os.path.splitext(os.path.basename(test_pdf))[0]
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            
            # --- FASE 3: Product Web Enrichment ---
            print("\n=== Avvio Stage 3: Product Web Enrichment ===")
            enriched_catalog = []
            
            for item in risultato:
                # Stage 3a
                res_3a = stage_3a_web_search(item, brand_name)
                raw_text = res_3a.get("raw_text", "")
                
                # Parsing
                parsed_text = parse_stage_3a_output(raw_text)
                
                # Stage 3b
                res_3b = stage_3b_fields_mapping(parsed_text, raw_text, allowed_categories, allowed_sex)
                
                # Stage 3c
                res_3c = stage_3c_free_form_completion(raw_text)
                
                final_item = {
                    "VendorCode": item.get("VendorCode"),
                    "Barcode": item.get("Barcode"),
                    "Quantity": item.get("Quantity"),
                    "llm_calls": {
                        "stage_3a": res_3a.pop("call_id_3a", None),
                        "stage_3b": res_3b.pop("call_id_3b", None),
                        "stage_3c": res_3c.pop("call_id_3c", None),
                    },
                    "sources": res_3a.get("urls", []),
                    **res_3b,
                    **res_3c
                }
                enriched_catalog.append(final_item)
                
            print("\n--- Dati Arricchiti (Stage 3) ---")
            print(json.dumps(enriched_catalog, indent=4, ensure_ascii=False))
            
            enriched_filename = os.path.join(output_dir, f"{base_name}_{timestamp}_enriched.json")
            with open(enriched_filename, "w", encoding="utf-8") as out_f:
                json.dump(enriched_catalog, out_f, indent=4, ensure_ascii=False)
            print(f"\nEstrazione ARRICCHITA salvata con successo in: {enriched_filename}")
            
            # --- SALVATAGGIO LOG LLM ---
            llm_logs_dir = "llm_usage_logs"
            os.makedirs(llm_logs_dir, exist_ok=True)
            llm_log_filename = os.path.join(llm_logs_dir, f"{base_name}_{timestamp}_llm_log.json")
            with open(llm_log_filename, "w", encoding="utf-8") as log_f:
                json.dump(LLM_LOGS, log_f, indent=4, ensure_ascii=False)
            print(f"\nLog di utilizzo LLM salvato con successo in: {llm_log_filename}")
            
    else:
        print(f"Errore: Il file '{test_pdf}' non è presente nella cartella corrente.")