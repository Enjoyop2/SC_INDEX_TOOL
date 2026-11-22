# SC_INDEX_TOOL
Supercell is a tool that converts and reconverts Scindex (a type of flatbuffer) into the new data format csv.

## ⚙️ How It Works

The tool operates in two primary modes:

### 1. Decode (Extraction & CSV Generation)
When the `decode` command is executed, the tool follows these steps:
1. **Schema Detection:** Checks whether the target `.scindex` file contains embedded CSV data.
   * **If CSVs exist:** Uses the existing CSVs directly as the data schema.
   * **If no CSVs exist:** Analyzes the `.scindex` structure and attempts to automatically generate a new CSV schema.
2. **Export:** Saves all resulting CSV files into the `csv/` directory.

### 2. Encode (Repacking)
When the `encode` command is executed, the tool reads the modified tables from the `csv/` directory and packs them back into the `.scindex` format following the detected schema structure.

## 💻 Usage

Run the tool via terminal using either the `decode` or `encode` argument:

### Decode `.scindex` to CSV
Extract data and save the output files to the `csv/` folder:
```bash
python3 main.py decode
```

### Encode CSV to `.scindex`
Repack the CSV files from the `csv/` folder back into an index file:
```bash
python3 main.py encode
```

## 📁 Project Structure

```text
SC_INDEX_TOOL/
├── main.py          # Main entry point (handles decode and encode workflows)
├── csv/             # Directory where decoded/ CSV files are stored
└── scindex/        # Directory where encoded/ scindex files are stored
```

## 🤝 Contributing

Contributions are welcome! Feel free to open an *Issue* to report bugs or submit a *Pull Request* for improvements.
