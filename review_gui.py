import http.server
import socketserver
import json
import yaml
import os
import webbrowser
import threading
from pathlib import Path

# Configuration
CONFIG_FILE = "config.yaml"
PORT = 8080

def get_data_file_path():
    try:
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            config = yaml.safe_load(f)
            # Try to get raw_data_extraction_file, fallback to a hardcoded one if it doesn't exist for testing
            file_path = config.get('raw_data_extraction_file')
            if not file_path:
                print("Warning: 'raw_data_extraction_file' not found in config.yaml. Please add it.")
                return None
            return file_path
    except Exception as e:
        print(f"Error reading config.yaml: {e}")
        return None

DATA_FILE = get_data_file_path()

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Data Extraction Review</title>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600&display=swap" rel="stylesheet">
    <style>
        :root {
            --bg-color: #0f172a;
            --surface-color: #1e293b;
            --border-color: #334155;
            --text-primary: #f8fafc;
            --text-secondary: #94a3b8;
            --accent-color: #3b82f6;
            --accent-hover: #2563eb;
            --success-color: #10b981;
            --danger-color: #ef4444;
        }

        body {
            font-family: 'Inter', sans-serif;
            background-color: var(--bg-color);
            color: var(--text-primary);
            margin: 0;
            padding: 20px;
            box-sizing: border-box;
        }

        header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 24px;
            padding-bottom: 16px;
            border-bottom: 1px solid var(--border-color);
        }

        h1 {
            font-size: 1.5rem;
            font-weight: 600;
            margin: 0;
        }

        .file-info {
            color: var(--text-secondary);
            font-size: 0.875rem;
            margin-top: 4px;
        }

        .btn {
            background-color: var(--accent-color);
            color: white;
            border: none;
            padding: 10px 20px;
            border-radius: 6px;
            font-size: 0.875rem;
            font-weight: 500;
            cursor: pointer;
            transition: background-color 0.2s, transform 0.1s;
            display: flex;
            align-items: center;
            gap: 8px;
        }

        .btn:hover {
            background-color: var(--accent-hover);
        }

        .btn:active {
            transform: translateY(1px);
        }

        .btn-save {
            background-color: var(--success-color);
        }

        .btn-save:hover {
            background-color: #059669;
        }

        .table-container {
            overflow-x: auto;
            background-color: var(--surface-color);
            border-radius: 8px;
            border: 1px solid var(--border-color);
            box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.1), 0 2px 4px -1px rgba(0, 0, 0, 0.06);
        }

        table {
            width: 100%;
            border-collapse: collapse;
            text-align: left;
            font-size: 0.875rem;
        }

        th, td {
            padding: 12px 16px;
            border-bottom: 1px solid var(--border-color);
            vertical-align: top;
        }

        th {
            background-color: #0f172a;
            font-weight: 600;
            color: var(--text-secondary);
            position: sticky;
            top: 0;
            white-space: nowrap;
            z-index: 10;
        }

        tbody tr:hover {
            background-color: rgba(255, 255, 255, 0.02);
        }

        .wrap-text {
            min-width: 150px;
            max-width: 400px;
            white-space: normal;
            word-wrap: break-word;
        }

        /* Form elements */
        .checkbox-wrapper {
            display: flex;
            align-items: center;
            justify-content: center;
        }

        input[type="checkbox"] {
            appearance: none;
            background-color: transparent;
            margin: 0;
            font: inherit;
            color: currentColor;
            width: 1.15em;
            height: 1.15em;
            border: 2px solid var(--text-secondary);
            border-radius: 0.15em;
            display: grid;
            place-content: center;
            cursor: pointer;
            transition: border-color 0.2s;
        }

        input[type="checkbox"]::before {
            content: "";
            width: 0.65em;
            height: 0.65em;
            transform: scale(0);
            transition: 120ms transform ease-in-out;
            box-shadow: inset 1em 1em var(--success-color);
            background-color: var(--success-color);
            transform-origin: center;
            clip-path: polygon(14% 44%, 0 65%, 50% 100%, 100% 16%, 80% 0%, 43% 62%);
        }

        input[type="checkbox"]:checked::before {
            transform: scale(1);
        }
        
        input[type="checkbox"]:checked {
            border-color: var(--success-color);
        }

        textarea {
            width: 100%;
            min-width: 150px;
            min-height: 40px;
            background-color: rgba(0, 0, 0, 0.2);
            border: 1px solid var(--border-color);
            color: var(--text-primary);
            border-radius: 4px;
            padding: 8px;
            font-family: inherit;
            font-size: 0.875rem;
            resize: vertical;
            transition: border-color 0.2s;
        }

        textarea:focus {
            outline: none;
            border-color: var(--accent-color);
        }
        
        .array-item {
            display: inline-block;
            background: rgba(255,255,255,0.1);
            padding: 2px 6px;
            border-radius: 4px;
            margin: 2px;
            font-size: 0.75rem;
        }
        
        /* Toast Notification */
        #toast {
            visibility: hidden;
            min-width: 250px;
            background-color: var(--success-color);
            color: #fff;
            text-align: center;
            border-radius: 4px;
            padding: 12px;
            position: fixed;
            z-index: 100;
            left: 50%;
            bottom: 30px;
            transform: translateX(-50%);
            font-size: 0.875rem;
            box-shadow: 0 4px 6px rgba(0,0,0,0.2);
        }

        #toast.show {
            visibility: visible;
            animation: fadein 0.5s, fadeout 0.5s 2.5s;
        }

        @keyframes fadein {
            from {bottom: 0; opacity: 0;}
            to {bottom: 30px; opacity: 1;}
        }

        @keyframes fadeout {
            from {bottom: 30px; opacity: 1;}
            to {bottom: 0; opacity: 0;}
        }

    </style>
</head>
<body>

    <header>
        <div>
            <h1>Data Extraction Review</h1>
            <div class="file-info" id="file-info">Loading...</div>
        </div>
        <button class="btn btn-save" onclick="saveData()">
            <svg width="16" height="16" fill="none" stroke="currentColor" viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 7H5a2 2 0 00-2 2v9a2 2 0 002 2h14a2 2 0 002-2V9a2 2 0 00-2-2h-3m-1 4l-3 3m0 0l-3-3m3 3V4"></path></svg>
            Save Reviewed File
        </button>
    </header>

    <div class="table-container">
        <table>
            <thead>
                <tr>
                    <th>Vendor Code</th>
                    <th>Product Name</th>
                    <th>Short Description</th>
                    <th>Extended Description</th>
                    <th>Quantity</th>
                    <th>Category</th>
                    <th>Sub Category</th>
                    <th>Sex</th>
                    <th>Materials</th>
                    <th>Colors</th>
                    <th>Tags</th>
                    <th>Barcode</th>
                    <th>Correct</th>
                    <th>Comment</th>
                </tr>
            </thead>
            <tbody id="table-body">
                <!-- Rows injected via JS -->
            </tbody>
        </table>
    </div>
    
    <div id="toast">Data saved successfully!</div>

    <script>
        let tableData = [];

        async function loadData() {
            try {
                const response = await fetch('/data');
                if (!response.ok) throw new Error('Failed to fetch data');
                
                tableData = await response.json();
                
                if (tableData.error) {
                    document.getElementById('file-info').innerText = 'Error: ' + tableData.error;
                    return;
                }
                
                document.getElementById('file-info').innerText = `Loaded ${tableData.length} entries from extraction file`;
                renderTable();
            } catch (error) {
                console.error(error);
                document.getElementById('file-info').innerText = 'Error loading data. Check console.';
            }
        }

        function formatArray(arr) {
            if (!arr || !Array.isArray(arr)) return arr || '-';
            return arr.map(item => `<span class="array-item">${item}</span>`).join('');
        }

        function renderTable() {
            const tbody = document.getElementById('table-body');
            tbody.innerHTML = '';

            tableData.forEach((row, index) => {
                const tr = document.createElement('tr');
                
                // Determine if it was already marked
                const isCorrect = row.correct === true;
                const comment = row.comment || '';

                tr.innerHTML = `
                    <td>${row.VendorCode || '-'}</td>
                    <td><div class="wrap-text">${row.product_name || '-'}</div></td>
                    <td><div class="wrap-text">${row.product_short_description || '-'}</div></td>
                    <td><div class="wrap-text">${row.product_extended_description || '-'}</div></td>
                    <td>${row.Quantity || '-'}</td>
                    <td>${row.category || '-'}</td>
                    <td>${row.sub_category || '-'}</td>
                    <td>${row.sex || '-'}</td>
                    <td>${formatArray(row.materials)}</td>
                    <td>${formatArray(row.colors)}</td>
                    <td>${formatArray(row.tags)}</td>
                    <td>${row.Barcode || '-'}</td>
                    <td>
                        <div class="checkbox-wrapper">
                            <input type="checkbox" onchange="updateRow(${index}, 'correct', this.checked)" ${isCorrect ? 'checked' : ''}>
                        </div>
                    </td>
                    <td>
                        <textarea placeholder="Add a comment..." onchange="updateRow(${index}, 'comment', this.value)">${comment}</textarea>
                    </td>
                `;
                tbody.appendChild(tr);
            });
        }

        function updateRow(index, field, value) {
            tableData[index][field] = value;
        }

        async function saveData() {
            try {
                const response = await fetch('/save', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json'
                    },
                    body: JSON.stringify(tableData)
                });
                
                if (response.ok) {
                    showToast();
                } else {
                    alert('Failed to save data!');
                }
            } catch (error) {
                console.error(error);
                alert('An error occurred while saving.');
            }
        }
        
        function showToast() {
            const toast = document.getElementById("toast");
            toast.className = "show";
            setTimeout(function(){ toast.className = toast.className.replace("show", ""); }, 3000);
        }

        // Initialize
        loadData();
    </script>
</body>
</html>
"""

class ReviewHandler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/':
            self.send_response(200)
            self.send_header('Content-type', 'text/html')
            self.end_headers()
            self.wfile.write(HTML_TEMPLATE.encode('utf-8'))
            
        elif self.path == '/data':
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            
            data_file = get_data_file_path()
            if not data_file or not os.path.exists(data_file):
                self.wfile.write(json.dumps({"error": "Data file not found or not configured in config.yaml"}).encode('utf-8'))
                return
                
            with open(data_file, 'r', encoding='utf-8') as f:
                self.wfile.write(f.read().encode('utf-8'))
                
        else:
            self.send_error(404)

    def do_POST(self):
        if self.path == '/save':
            content_length = int(self.headers.get('Content-Length', 0))
            post_data = self.rfile.read(content_length)
            
            try:
                data = json.loads(post_data.decode('utf-8'))
                data_file = get_data_file_path()
                
                if data_file:
                    base, ext = os.path.splitext(data_file)
                    output_file = f"{base}_reviewed{ext}"
                    
                    with open(output_file, 'w', encoding='utf-8') as f:
                        json.dump(data, f, indent=4, ensure_ascii=False)
                        
                    self.send_response(200)
                    self.send_header('Content-type', 'application/json')
                    self.end_headers()
                    self.wfile.write(json.dumps({"status": "ok", "saved_to": output_file}).encode('utf-8'))
                else:
                    self.send_error(500, "Configuration missing")
            except Exception as e:
                self.send_error(500, str(e))
        else:
            self.send_error(404)

def start_server():
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer(("", PORT), ReviewHandler) as httpd:
        print(f"Serving GUI at http://localhost:{PORT}")
        print("Press Ctrl+C to stop the server.")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nShutting down server...")
            httpd.server_close()

if __name__ == "__main__":
    # Open the browser in a separate thread so it doesn't block the server startup
    t = threading.Thread(target=lambda: webbrowser.open(f'http://localhost:{PORT}'))
    t.daemon = True
    t.start()
    start_server()
