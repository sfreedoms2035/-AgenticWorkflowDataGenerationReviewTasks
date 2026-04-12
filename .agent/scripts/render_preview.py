"""
render_preview.py — HTML Preview Generator for Expert Reviews
============================================================
Converts generated review tasks (JSON) into a simple, structurally clear HTML 
page without complex CSS, focusing on readability of the findings and artifacts.
"""
import sys
import json
import os
import webbrowser
import markdown

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PREVIEW_DIR = os.path.join(BASE_DIR, "Eval", "Previews")

def generate_html(json_path):
    with open(json_path, 'r', encoding='utf-8') as f:
        data = json.load(f)[0]
    
    metadata = data.get("metadata", {})
    conversations = data.get("conversations", [])
    
    html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<title>Preview: {metadata.get('training_data_id', 'Unknown')}</title>
<style>
    body {{ font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif; line-height: 1.6; max-width: 1200px; margin: 0 auto; padding: 20px; color: #333; }}
    h1, h2, h3 {{ color: #111; border-bottom: 1px solid #ccc; padding-bottom: 5px; }}
    .meta-box {{ background: #f9f9f9; padding: 15px; border: 1px solid #ddd; border-radius: 5px; margin-bottom: 20px; }}
    .co {{ padding: 15px; margin-bottom: 20px; border-radius: 5px; }}
    .user {{ background: #f0f7ff; border-left: 4px solid #0066cc; }}
    .assistant {{ background: #fdfdfd; border-left: 4px solid #cc0000; }}
    .finding-card {{ border: 1px solid #ddd; margin-bottom: 10px; padding: 10px; border-radius: 5px; background: #fff; }}
    .finding-header {{ font-weight: bold; margin-bottom: 5px; }}
    table {{ border-collapse: collapse; width: 100%; margin-bottom: 15px; }}
    th, td {{ border: 1px solid #ccc; padding: 8px; text-align: left; }}
    th {{ background: #f0f0f0; }}
    pre {{ background: #f4f4f4; padding: 10px; overflow-x: auto; border-radius: 5px; }}
</style>
</head>
<body>
<h1>Review Task Preview</h1>
<div class="meta-box">
    <strong>ID:</strong> {metadata.get('training_data_id')}<br>
    <strong>Document:</strong> {metadata.get('document')}<br>
    <strong>Role:</strong> {metadata.get('affected_role')}<br>
    <strong>Strategy:</strong> {metadata.get('summary')}
</div>

<h2>Conversation Log</h2>
"""
    
    for idx, conv in enumerate(conversations):
        role_class = "user" if conv["role"] == "user" else "assistant"
        html += f'<div class="co {role_class}">\n'
        html += f'<h3>Turn {idx + 1} - {conv["role"].capitalize()}</h3>\n'
        
        # If assistant, maybe there's reasoning
        if 'reasoning' in conv and conv['reasoning'] != '<think></think>':
            html += f"<details><summary>Thought Process (Click to expand)</summary><div style='padding:10px;background:#eee;margin-top:10px;'>"
            html += markdown.markdown(conv['reasoning'])
            html += f"</div></details>\n<hr>\n"
            
        content_text = conv.get("content", "")
        
        # In Turn 2, content is often JSON structure of the Review
        if idx == 1:
            try:
                # Try to parse the content as JSON (sometimes it is stringified JSON due to how playwright pipeline extracts it)
                parsed_content = json.loads(content_text) if isinstance(content_text, str) else content_text
                
                if isinstance(parsed_content, dict):
                    html += "<h4>Review Metadata</h4>"
                    html += markdown.markdown(str(parsed_content.get("review_metadata", "")))
                    
                    html += "<h4>Review Criteria</h4>"
                    html += markdown.markdown(str(parsed_content.get("review_criteria", "")))
                    
                    findings = parsed_content.get("findings", [])
                    html += f"<h4>Findings ({len(findings)})</h4>"
                    for fnd in findings:
                        html += f"<div class='finding-card'>"
                        html += f"<div class='finding-header'>[{fnd.get('id', 'N/A')}] {fnd.get('classification', 'N/A')}</div>"
                        html += f"<div><strong>Description:</strong> {fnd.get('description', '')}</div>"
                        html += f"<div><strong>Recommendation:</strong> {fnd.get('recommendation', '')}</div>"
                        html += f"</div>"
                        
                    html += "<h4>Overall Assessment</h4>"
                    html += markdown.markdown(str(parsed_content.get("overall_assessment", "")))
                    
                    html += "<h4>Rewritten Corrected Artifact</h4>"
                    html += f"<div style='background:#ffffee; padding:15px; border:1px solid #ecec00; white-space:pre-wrap;'>"
                    html += markdown.markdown(str(parsed_content.get("rewritten_corrected_artifact", "")))
                    html += f"</div>"
                    
                else:
                    html += markdown.markdown(str(parsed_content))
            except Exception:
                html += markdown.markdown(content_text)
        else:
            html += markdown.markdown(content_text)
            
        html += f"</div>\n"
        
    html += "</body></html>"
    
    os.makedirs(PREVIEW_DIR, exist_ok=True)
    basename = os.path.basename(json_path).replace('.json', '.html')
    output_path = os.path.join(PREVIEW_DIR, basename)
    
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(html)
        
    return output_path

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python render_preview.py <path_to_json> [--open]")
        sys.exit(1)
        
    json_path = sys.argv[1]
    out_path = generate_html(json_path)
    print(f"Generated preview: {out_path}")
    
    if "--open" in sys.argv:
        webbrowser.open(f'file:///{out_path.replace(os.sep, "/")}')
