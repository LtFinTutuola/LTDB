import time
import os
import json
import pdfplumber
from google import genai
from google.genai import types

import yaml

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

def extract_raw_tables_locally(pdf_path):
    """
    Esegue il pre-processing locale del PDF estraendo le tabelle grezze.
    Restituisce una lista di righe (che convertiremo in JSON testuale).
    """
    print(f"Avvio pre-processing locale con pdfplumber sul file '{pdf_path}'...")
    raw_table_data = []
    
    try:
        with pdfplumber.open(pdf_path) as pdf:
            for i, page in enumerate(pdf.pages):
                # Estrae le tabelle dalla pagina (restituisce una lista di liste)
                tables = page.extract_tables()
                for table in tables:
                    for row in table:
                        # Rimuove i valori None e pulisce le stringhe base
                        cleaned_row = [str(cell).replace('\n', ' ').strip() if cell else "" for cell in row]
                        # Teniamo solo le righe che non sono completamente vuote
                        if any(cleaned_row):
                            raw_table_data.append(cleaned_row)
                            
        print(f"Pre-processing completato: estratte {len(raw_table_data)} righe grezze.")
        return raw_table_data
    except Exception as e:
        print(f"Errore durante l'estrazione locale: {str(e)}")
        return None

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
            # Salva il risultato nella cartella raw_data_extractions
            output_dir = "raw_data_extractions"
            os.makedirs(output_dir, exist_ok=True)
            
            base_name = os.path.splitext(os.path.basename(test_pdf))[0]
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            output_filename = os.path.join(output_dir, f"{base_name}_{timestamp}.json")
            
            with open(output_filename, "w", encoding="utf-8") as out_f:
                json.dump(risultato, out_f, indent=4, ensure_ascii=False)
            print(f"\nEstrazione salvata con successo in: {output_filename}")
            
    else:
        print(f"Errore: Il file '{test_pdf}' non è presente nella cartella corrente.")