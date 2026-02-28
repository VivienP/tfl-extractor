import fitz
import json
import logging
import argparse
import re
import os
import sys
import csv
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass

def setup_logger(verbose: bool) -> logging.Logger:
    logger = logging.getLogger("tfl_extractor")
    if not logger.handlers:
        handler = logging.StreamHandler()
        formatter = logging.Formatter('%(levelname)s: %(message)s')
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    logger.setLevel(logging.INFO if verbose else logging.WARNING)
    return logger

def parse_tfl_page(text: str) -> Dict[str, str]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    tfl_id = None
    tfl_type = None
    title = ""
    id_line_idx = -1
    
    for i, line in enumerate(lines[:10]):
        if re.match(r'^(Table|Figure)\s*14[.\-][\d.\-]+', line, re.IGNORECASE):
            id_line_idx = i
            tfl_id = line
            tfl_type = "table" if "table" in line.lower() else "figure"
            break
            
    if id_line_idx != -1 and id_line_idx + 1 < len(lines):
        title = lines[id_line_idx + 1]
        
    population = ""
    for line in lines[:10]:
        if line.lower().startswith("population:"):
            population = line.split(":", 1)[1].strip()
            break
            
    source_program = ""
    for line in reversed(lines[-15:]):
        if line.lower().startswith("source:"):
            part = line[len("source:"):].strip()
            parts = re.split(r'\s{2,}', part)
            if parts:
                source_program = parts[0]
            break
            
    return {
        "id": tfl_id,
        "type": tfl_type,
        "title": title,
        "population": population,
        "source_program": source_program
    }

def extract_txt(doc: fitz.Document, start_page: int, end_page: int, filepath: str, logger: logging.Logger) -> None:
    try:
        with open(filepath, 'w', encoding='utf-8') as f:
            for i in range(start_page, end_page + 1):
                page = doc[i]
                text = page.get_text("text")
                f.write(text)
                if i < end_page:
                    f.write(f"\n--- Page {i - start_page + 2} ---\n")
    except Exception as e:
        logger.warning(f"Failed to extract text to {filepath}: {e}")
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(f"[TEXT EXTRACTION FAILED ON PAGE {start_page + 1}]")

def run_validation(output_dir: str, logger: logging.Logger) -> None:
    manifest_path = os.path.join(output_dir, "manifest.json")
    if not os.path.exists(manifest_path):
        logger.error(f"Cannot validate: manifest.json not found in {output_dir}")
        return

    try:
        with open(manifest_path, "r", encoding="utf-8") as f:
            manifest = json.load(f)
    except json.JSONDecodeError:
        logger.error("Invalid manifest.json format")
        return

    print("\n=== Validation ===")
    
    checks = {
        "files_exist": True,
        "files_non_empty": True,
        "page_count_match": True,
        "no_page_gaps": True,
        "no_page_overlaps": True,
        "narrative_ok": True,
        "pdfs_readable": True
    }
    msgs: List[str] = []

    def log_fail(check_key: str, msg: str):
        checks[check_key] = False
        msgs.append(f"\u274c {msg}")

    # Narrative validation
    narrative = manifest.get("narrative", {})
    n_pages = narrative.get("page_count", 0)
    if narrative:
        n_file = os.path.join(output_dir, narrative.get("file", ""))
        if not os.path.exists(n_file):
            log_fail("narrative_ok", f"Narrative body missing: {n_file}")
            checks["files_exist"] = False
        elif os.path.getsize(n_file) == 0:
            log_fail("narrative_ok", f"Narrative body is empty: {n_file}")
            checks["files_non_empty"] = False
        else:
            try:
                n_doc = fitz.open(n_file)
                if len(n_doc) != n_pages:
                    log_fail("narrative_ok", f"Narrative page count mismatch: {len(n_doc)} != {n_pages}")
                n_doc.close()
            except Exception:
                log_fail("narrative_ok", f"Narrative body is unreadable: {n_file}")
                checks["pdfs_readable"] = False

    tlfs = manifest.get("tlfs", [])
    expected_next_page = None
    
    for tfl in tlfs:
        start_p, end_p = tfl.get("pages_in_source", [0, 0])
        if expected_next_page is not None:
            if start_p > expected_next_page:
                log_fail("no_page_gaps", f"Page gap before {tfl['id']} (expected {expected_next_page}, got {start_p})")
            elif start_p < expected_next_page:
                log_fail("no_page_overlaps", f"Page overlap at {tfl['id']} (expected {expected_next_page}, got {start_p})")
        expected_next_page = end_p + 1

        f_path = os.path.join(output_dir, tfl.get("file", ""))
        p_count = tfl.get("page_count", 0)
        
        if not os.path.exists(f_path):
            log_fail("files_exist", f"File missing for {tfl['id']}: {f_path}")
            continue
            
        if os.path.getsize(f_path) == 0:
            log_fail("files_non_empty", f"File empty for {tfl['id']}: {f_path}")
            continue
            
        try:
            t_doc = fitz.open(f_path)
            if len(t_doc) != p_count:
                log_fail("page_count_match", f"Page count mismatch: {os.path.basename(f_path)} has {len(t_doc)} pages but manifest says {p_count}")
            t_doc.close()
        except Exception:
            log_fail("pdfs_readable", f"File unreadable for {tfl['id']}: {f_path}")

    def safe_print(msg):
        try:
            print(msg)
        except UnicodeEncodeError:
            print(msg.encode('ascii', 'replace').decode('ascii'))

    if checks["files_exist"]: safe_print(f"\u2705 All {len(tlfs)} TFL files exist")
    if checks["files_non_empty"]: safe_print("\u2705 All files non-empty")
    if checks["page_count_match"]: safe_print("\u2705 Page counts consistent")
    
    if checks["no_page_gaps"]:
        if tlfs:
            min_p = tlfs[0]["pages_in_source"][0]
            max_p = tlfs[-1]["pages_in_source"][1]
            safe_print(f"\u2705 No page gaps in Section 14 (pages {min_p}-{max_p} covered)")
        else:
            safe_print("\u2705 No page gaps in Section 14")
            
    if checks["no_page_overlaps"]: safe_print("\u2705 No page overlaps")
    if checks["narrative_ok"]: safe_print(f"\u2705 Narrative body OK ({n_pages} pages)")
    if checks["pdfs_readable"]: safe_print("\u2705 All PDFs readable")
        
    for m in msgs:
        safe_print(m)
        
    passed = sum(1 for v in checks.values() if v)
    total = len(checks)
    safe_print(f"\nValidation {'PASSED' if passed == total else 'FAILED'} ({passed}/{total} checks {'passed' if passed != total else ''})".strip())


def extract_tlfs(input_path: str, output_dir: str, verbose: bool, dry_run: bool, no_text: bool) -> None:
    logger = setup_logger(verbose)
    
    if not os.path.exists(input_path):
        logger.error(f"Input file not found: {input_path}")
        return
        
    pdf_dir = os.path.join(output_dir, "pdf")
    text_dir = os.path.join(output_dir, "text")
    
    if not dry_run:
        os.makedirs(output_dir, exist_ok=True)
        os.makedirs(pdf_dir, exist_ok=True)
        if not no_text:
            os.makedirs(text_dir, exist_ok=True)
            
    doc = fitz.open(input_path)
    total_pages = len(doc)
    doc_name = os.path.basename(input_path)
    
    if total_pages < 43:
        logger.error(f"Document has too few pages ({total_pages}). Cannot extract CSR.")
        return

    tlfs: List[Dict[str, Any]] = []
    current_tfl: Optional[Dict[str, Any]] = None
    warnings = 0
    tfl_pages_total = 0
    table_count = 0
    figure_count = 0

    if verbose:
        logger.info("Extracting narrative body (pages 1-42)...")
        
    narrative_file = "pdf/narrative_body.pdf"
    if not dry_run:
        narrative_pdf = fitz.open()
        narrative_pdf.insert_pdf(doc, from_page=0, to_page=41)
        narrative_pdf.save(os.path.join(output_dir, narrative_file))
        narrative_pdf.close()
        
        if not no_text:
            extract_txt(doc, 0, 41, os.path.join(output_dir, "text/narrative_body.txt"), logger)

    for page_num in range(42, total_pages):
        page = doc[page_num]
        text = page.get_text("text")

        lines = [line.strip() for line in text.splitlines() if line.strip()]
        is_termination = False
        for line in lines[:20]:
            upper_line = line.upper()
            if upper_line.startswith("15. REFERENCE") or upper_line == "15." or upper_line.startswith("16. APPEND") or upper_line == "16.":
                is_termination = True
                break
        
        if is_termination:
            if verbose:
                logger.info(f"Reached Section 15/16 on page {page_num + 1}. Terminating TFL extraction.")
            break
            
        parsed = parse_tfl_page(text)
        tfl_id = parsed["id"]
        
        if not tfl_id:
            if current_tfl:
                current_tfl['pages_in_source'][1] = page_num + 1
                current_tfl['page_count'] += 1
                tfl_pages_total += 1
            else:
                if len(tlfs) == 0:
                    logger.warning(f"No clear TFL ID found on page {page_num + 1} before any TFL started.")
                    warnings += 1
            continue
            
        if current_tfl and current_tfl['id'] == tfl_id:
            current_tfl['pages_in_source'][1] = page_num + 1
            current_tfl['page_count'] += 1
            tfl_pages_total += 1
        else:
            if verbose and current_tfl:
                logger.info(f"Found {current_tfl['id']} (Pages {current_tfl['pages_in_source'][0]}-{current_tfl['pages_in_source'][1]})")
                
            safe_id = tfl_id.replace(" ", "_").replace("\n", "")
            out_file_name = f"pdf/{safe_id}.pdf"
            
            current_tfl = {
                "id": tfl_id,
                "type": parsed["type"],
                "title": parsed["title"],
                "file": out_file_name,
                "pages_in_source": [page_num + 1, page_num + 1],
                "page_count": 1,
                "population": parsed["population"]
            }
            if parsed["source_program"]:
                current_tfl["source_program"] = parsed["source_program"]
                
            tlfs.append(current_tfl)
            tfl_pages_total += 1
            
            if parsed["type"] == "table":
                table_count += 1
            else:
                figure_count += 1

    if verbose and current_tfl:
        logger.info(f"Found {current_tfl['id']} (Pages {current_tfl['pages_in_source'][0]}-{current_tfl['pages_in_source'][1]})")

    if not dry_run:
        for tfl in tlfs:
            start_idx = tfl["pages_in_source"][0] - 1
            end_idx = tfl["pages_in_source"][1] - 1
            
            tfl_pdf_out = fitz.open()
            tfl_pdf_out.insert_pdf(doc, from_page=start_idx, to_page=end_idx)
            out_file_path = os.path.join(output_dir, tfl["file"])
            tfl_pdf_out.save(out_file_path)
            tfl_pdf_out.close()

            if not no_text:
                safe_id = tfl["id"].replace(" ", "_").replace("\n", "")
                text_file_path = os.path.join(output_dir, f"text/{safe_id}.txt")
                extract_txt(doc, start_idx, end_idx, text_file_path, logger)

    manifest = {
        "source_file": doc_name,
        "source_pages": total_pages,
        "extraction_date": datetime.now(timezone.utc).isoformat()[:-13] + "Z",
        "narrative": {
            "file": "pdf/narrative_body.pdf",
            "pages_in_source": [1, 42],
            "page_count": 42
        },
        "tlfs": tlfs
    }
    
    if not dry_run:
        json_path = os.path.join(output_dir, "manifest.json")
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2)
            
        csv_path = os.path.join(output_dir, "manifest.csv")
        with open(csv_path, "w", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["id", "type", "title", "file", "pages_in_source_start", "pages_in_source_end", "page_count", "population", "source_program"])
            
            n = manifest["narrative"]
            writer.writerow(["narrative_body", "narrative", "", n["file"], n["pages_in_source"][0], n["pages_in_source"][1], n["page_count"], "", ""])
            
            for tfl in tlfs:
                writer.writerow([
                    tfl.get("id", ""),
                    tfl.get("type", ""),
                    tfl.get("title", ""),
                    tfl.get("file", ""),
                    tfl["pages_in_source"][0],
                    tfl["pages_in_source"][1],
                    tfl.get("page_count", 0),
                    tfl.get("population", ""),
                    tfl.get("source_program", "")
                ])
                
    def safe_print(msg):
        try:
            print(msg)
        except UnicodeEncodeError:
            print(msg.encode('ascii', 'replace').decode('ascii'))

    safe_print("\n=== Extraction Summary ===")
    safe_print(f"Source: {doc_name} ({total_pages} pages)")
    safe_print("Narrative: pages 1-42 -> pdf/narrative_body.pdf")
    safe_print(f"TFLs extracted: {len(tlfs)} ({table_count} tables, {figure_count} figure{'s' if figure_count != 1 else ''})")
    safe_print(f"Total TFL pages: {tfl_pages_total}")
    safe_print(f"Warnings: {warnings}\n")
    
    for tfl in tlfs:
        id_str = tfl['id'].ljust(14)
        page_str = f"({tfl['page_count']}p)".ljust(6)
        safe_print(f"{id_str} {page_str} {tfl['title']}")

def main() -> None:
    parser = argparse.ArgumentParser(description="Extract TFLs from Generic ICH E3 CSR PDF.")
    parser.add_argument("--input", type=str, help="Path to the input PDF file")
    parser.add_argument("--output", type=str, required=True, help="Directory to save the extracted outputs")
    parser.add_argument("--verbose", action="store_true", help="Print verbose extraction steps")
    parser.add_argument("--dry-run", action="store_true", help="Detect without creating files")
    parser.add_argument("--validate", action="store_true", help="Run validation on output directory")
    parser.add_argument("--no-text", action="store_true", help="Skip text extraction")
    
    args = parser.parse_args()
    
    if args.validate and not args.input:
        logger = setup_logger(args.verbose)
        run_validation(args.output, logger)
        return
        
    if not args.input:
        parser.error("--input is required unless running standalone --validate")
        
    extract_tlfs(args.input, args.output, args.verbose, args.dry_run, args.no_text)
    
    if args.validate and not args.dry_run:
        logger = setup_logger(args.verbose)
        run_validation(args.output, logger)

if __name__ == "__main__":
    main()
