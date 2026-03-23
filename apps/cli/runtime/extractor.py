import os
import re
from typing import Dict, Any, Optional, List

class LocalExtractor:
    def __init__(self, cwd: str):
        self.cwd = cwd

    def inspect_file(self, path: str, query: str) -> str:
        """Structured file inspection without LLM inference."""
        full_path = os.path.join(self.cwd, path)
        if not os.path.exists(full_path):
            return f"✘ Error: File {path} not found in {self.cwd}."
            
        try:
            with open(full_path, "r", encoding="utf-8-sig", newline="") as f:
                content = f.read()
            
            lines = content.splitlines()
            total_lines = len(lines)
            first_line = lines[0] if lines else "EMPTY"
            
            # Find version if present
            v_line = next((l for l in lines if "version" in l.lower()), "N/A")
            
            found_line, found_index = "No encontrado", "N/A"
            # Pattern search
            search_pattern = ""
            if any(k in query.lower() for k in ["aparece", "donde", "contiene"]):
                code_match = re.search(r'`(.*?)`', query)
                if code_match:
                    search_pattern = code_match.group(1)
                else:
                    parts = re.split(r'aparece|donde|contiene', query, flags=re.IGNORECASE)
                    if len(parts) > 1:
                        lp = parts[-1].strip()
                        search_pattern = re.split(r'usa datos|no inventes', lp, flags=re.IGNORECASE)[0].strip()
                        search_pattern = search_pattern.strip('`"\' ').rstrip('.?!')
                
                if search_pattern:
                    for i, line in enumerate(lines):
                        if search_pattern in line:
                            found_line = line.strip()
                            found_index = i + 1
                            break
            
            return {
                "first_line": first_line,
                "total_lines": total_lines,
                "version_line": v_line,
                "found_line": found_line,
                "found_index": found_index,
                "path": path
            }
        except Exception as e:
            return {"error": str(e)}

    def analyze_text_block(self, text: str) -> Dict[str, Any]:
        """Verify the structural integrity of a pasted block."""
        return {
            "char_count": len(text),
            "line_count": len(text.splitlines()) if text else 0,
            "newline_count": text.count('\n'),
            "last_line": text.splitlines()[-1] if text.splitlines() else ""
        }

    def should_bypass(self, text: str) -> bool:
        """Determines if a query should bypass the LLM for local extraction."""
        triggers = ["total de líneas", "conteo", "qué dice la línea", "última línea", "primeras n líneas"]
        return any(t in text.lower() for t in triggers)
