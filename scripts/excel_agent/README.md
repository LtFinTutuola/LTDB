# LTDB Edge Forwarder (Standalone x86 Windows)

Micro-agent leggero in Python 3.12 per l'acquisizione distribuita e la sincronizzazione in tempo reale delle vendite da file Excel verso LTDB (AWS).

---

## 1. Requisiti di Sistema
- **Sistema Operativo**: Windows (x86 / x64).
- **Python**: 3.12 (già installato sul PC aziendale).
- **Dipendenze pip**: Esclusivamente `openpyxl`.
  ```cmd
  pip install openpyxl
  ```

---

## 2. Installazione sul PC Aziendale

1. Copiare l'intera cartella `excel_agent\` sul PC aziendale in un percorso fisso (es. `C:\LTDB_Agent\`).
2. Aprire `config.json` e verificare i seguenti parametri:
   - `ltdb_base_url`: impostare l'indirizzo IP statico o DNS dell'istanza AWS EC2 di LTDB (es. `http://1.2.3.4:8000`).
   - `fallback_config.excel_file_path`: impostare il percorso reale del file Excel delle vendite sul PC locale (es. `C:\Dati\Stat € B.xlsx`).

---

## 3. Avvio Automatico all'Accensione di Windows

1. Premere `Win + R`, digitare `shell:startup` e premere Invio (si aprirà la cartella *Esecuzione automatica* dell'utente).
2. Creare un **collegamento** a `run_agent.bat` e incollarlo all'interno della cartella `shell:startup`.
3. Al prossimo avvio di Windows (o facendo doppio clic sul collegamento), lo script verrà eseguito in modalità silenziosa (`pythonw.exe`), senza aprire finestre visibili della console.

---

## 4. Struttura dei File Locali e Diagnostica

Lo script genera in autonomia due cartelle:
- `logs/`: Contiene i log giornalieri delle operazioni e di eventuali errori di rete (`agent_YYYY-MM-DD.log`).
- `state/`: Contiene gli hash delle righe sincronizzate per ogni data (`hashes_YYYY-MM-DD.json`).

I file di log e stato più vecchi di `retention_days` (default 30 giorni) vengono eliminati automaticamente all'avvio e a ogni mezzanotte.
