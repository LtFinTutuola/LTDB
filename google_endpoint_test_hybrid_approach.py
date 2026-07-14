import time
import os
import json
import uuid
import pdfplumber
from google import genai
from google.genai import types
from typing import List, Optional
from pydantic import BaseModel, Field

import yaml

# Global list to store LLM usage logs
LLM_LOGS = []

# =====================================================================
# DEFINIZIONE DELLO SCHEMA DI VALIDAZIONE RIGIDO (PYDANTIC)
# =====================================================================
class ProductSheet(BaseModel):
    product_name: str = Field(
        description="Official name of the product. DO NOT include the SKU or VendorCode in this string."
    )
    product_description: str = Field(
        description="Concise, technical, and precise description of the product to help an ERP operator identify it. NO commercial or promotional fluff."
    )
    category: str = Field(
        description="Product category. MUST be exactly one of the values provided in the prompt's Allowed Categories list."
    )
    sex: str = Field(
        description="Target gender for the product. MUST be exactly one of the values provided in the prompt's Allowed Sex list."
    )
    main_material: str = Field(
        description="Main material of the product. MUST be exactly one of the values provided in the prompt's Allowed Materials list."
    )
    secondary_material: Optional[str] = Field(
        description="Secondary or complementary material of the product. Leave null if not applicable. MUST be exactly one of the values provided in the prompt's Allowed Materials list.",
        default=None
    )
    primary_color: str = Field(
        description="The primary color of the product inferred from codes or web images."
    )
    secondary_color: Optional[str] = Field(
        description="Possible secondary color or chromatic details. Leave null if solid color.",
        default=None
    )
    tags: List[str] = Field(
        description="List of up to 10 descriptive tags related to the article (e.g., color, style, specific material details, usage) to enhance semantic search."
    )
    sources: List[str] = Field(
        description="Strict list of the full URLs of the web pages from which the data was extracted."
    )

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
        return full_text
    except Exception as e:
        print(f"Errore durante l'estrazione locale: {str(e)}")
        return None


def stage_3_enrich_product(mapped_item, brand_name, allowed_categories, allowed_sex, allowed_materials):
    """
    FASE 3: Web Grounding.
    Uses Gemini 3.1 Flash Lite with Google Search enabled and Pydantic validation
    to extract structured data from the web.
    """
    vendor_code = mapped_item.get('VendorCode', '')
    description = mapped_item.get('Description', '')
    color = mapped_item.get('Color', '')
    
    print(f"  -> Web search in progress for SKU: {vendor_code} ({brand_name})...")
    
    config = types.GenerateContentConfig(
        tools=[{"google_search": {}}],
        response_mime_type="application/json",
        response_schema=ProductSheet,
        system_instruction=(
            "You are an AI Agent specialized in Product Data Enrichment for a retail ERP system. "
            "Your goal is to browse the web, find the official technical sheet or e-commerce page "
            "of the requested product and rigorously fill out the output JSON. "
            "CRITICAL RULES:\n"
            "1. Use the Google search tool to find real specifications.\n"
            "2. Do not invent materials or descriptions. If a data point is not available online, write 'Dato non disponibile'.\n"
            "3. Always collect the exact URL of the page from which you took the information and insert it in the 'sources' array.\n"
            "4. Strictly respect the provided JSON schema.\n"
            "5. IMPORTANT: All output data values (descriptions, tags, categories, colors) MUST be written in Italian.\n"
            "6. DO NOT include the SKU or VendorCode inside the 'product_name'.\n"
            "7. 'category', 'sex', 'main_material', and 'secondary_material' MUST be populated using strictly one of the values provided in the Allowed lists.\n"
            "8. Populate 'tags' with up to 10 semantic keywords describing the item to enhance downstream search."
        )
    )

    prompt = (
        f"Find all technical and commercial specifications for this product.\n\n"
        f"--- STARTING DATA ---\n"
        f"Brand: {brand_name}\n"
        f"SKU / Model: {vendor_code}\n"
        f"Original Description: {description}\n"
        f"Color Code: {color}\n\n"
        f"--- ALLOWED MAPPING VALUES ---\n"
        f"Allowed Categories: {', '.join(allowed_categories)}\n"
        f"Allowed Sex: {', '.join(allowed_sex)}\n"
        f"Allowed Materials: {', '.join(allowed_materials)}\n\n"
        f"--- SUGGESTED TARGET SEARCH QUERY ---\n"
        f"\"{brand_name} {vendor_code}\" OR \"{brand_name} {description}\"\n\n"
        f"Analyze the web results and return the complete product sheet following the JSON schema."
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
        
        LLM_LOGS.append({
            "call_id": call_id,
            "pipeline_stage": "Stage 3 - Web Enrichment",
            "input_tokens": input_tokens,
            "output_tokens": output_tokens
        })
        
        parsed = json.loads(response.text)
        parsed["call_id"] = call_id
        return parsed
        

    except Exception as e:
        print(f"     [Error during enrichment for {vendor_code}]: {str(e)}")
        return {
            "product_name": description,
            "product_description": "Error during web enrichment.",
            "category": "Uncategorized",
            "material": "Unknown",
            "primary_color": color,
            "secondary_color": None,
            "sources": []
        }



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
        cleanup_config = types.GenerateContentConfig(
            response_mime_type="text/plain",
            system_instruction=(
                "Sei un assistente specializzato nell'estrazione dati. "
                "Il tuo compito è pulire il testo grezzo di una bolla di spedizione B2B. "
                "Rimuovi intestazioni, note legali, totali, dati bancari o qualsiasi altro dato "
                "che non riguardi strettamente gli articoli/prodotti spediti. "
                "Restituisci SOLO il testo relativo alle righe degli articoli, includendo anche le intestazioni delle colonne, senza alterare i dati in esse contenuti."
            )
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
            
            LLM_LOGS.append({
                "call_id": call_id_1,
                "pipeline_stage": "Stage 1 - Raw Text Cleanup",
                "input_tokens": input_tokens_1,
                "output_tokens": output_tokens_1
            })
        except Exception as error:
            return {"error": f"Errore durante la pulizia: {str(error)}"}

        print(f"Testo pulito generato:\n{cleaned_text}\n")

        # --- FASE 2: Mappatura in JSON ---
        print("Avvio mappatura JSON del testo pulito tramite LLM...")
        mapping_config = types.GenerateContentConfig(
            response_mime_type="application/json",
            system_instruction=(
                "Sei un Agente di Document Intelligence per un sistema ERP. "
                "Riceverai in input un testo pulito contenente solo gli articoli di una bolla di spedizione B2B. "
                "Il tuo compito è analizzare semanticamente questi dati "
                "e restituire ESCLUSIVAMENTE un array JSON di oggetti normalizzati con le seguenti chiavi: "
                "VendorCode (es. modello/codice prodotto), Barcode (se presente), Description (descrizione del prodotto), Color (codice/colore)."
            )
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
            
            LLM_LOGS.append({
                "call_id": call_id_2,
                "pipeline_stage": "Stage 2 - JSON Mapping",
                "input_tokens": input_tokens_2,
                "output_tokens": output_tokens_2
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
            allowed_materials = config.get("materials", [])
            
            # Salva il risultato RAW nella cartella raw_data_extractions
            output_dir = "raw_data_extractions"
            os.makedirs(output_dir, exist_ok=True)
            
            base_name = os.path.splitext(os.path.basename(test_pdf))[0]
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            raw_filename = os.path.join(output_dir, f"{base_name}_{timestamp}_raw.json")
            
            with open(raw_filename, "w", encoding="utf-8") as out_f:
                json.dump(risultato, out_f, indent=4, ensure_ascii=False)
            print(f"\nEstrazione RAW salvata con successo in: {raw_filename}")
            
            # --- FASE 3: Product Web Enrichment ---
            print("\n=== Avvio Stage 3: Product Web Enrichment ===")
            enriched_catalog = []
            
            for item in risultato:
                enriched_data = stage_3_enrich_product(item, brand_name, allowed_categories, allowed_sex, allowed_materials)
                final_item = {
                    "VendorCode": item.get("VendorCode"),
                    "Barcode": item.get("Barcode"),
                    **enriched_data
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