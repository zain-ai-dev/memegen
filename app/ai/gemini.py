import json
import asyncio
from pathlib import Path
from typing import Any
import random

import google.generativeai as genai
from sanic.log import logger

from .. import settings, utils, models
from .custom_templates import (
    scan_custom_templates,
    get_custom_templates_list,
    get_custom_templates_by_emotion,
    ensure_custom_templates_initialized
)


TEMPLATES_DIR = Path(settings.ROOT) / "templates"
try:
    _LAST_TEMPLATE_ID  # type: ignore[name-defined]
except NameError:
    _LAST_TEMPLATE_ID = None  # type: ignore[assignment]
try:
    _LAST_EXTENSION  # type: ignore[name-defined]
except NameError:
    _LAST_EXTENSION = None  # type: ignore[assignment]

# Query-based template tracking for variety
# Maps normalized query -> list of recently used template IDs
_QUERY_TEMPLATE_HISTORY: dict[str, list[str]] = {}
_MAX_HISTORY_PER_QUERY = 10  # Track last 10 templates per query
_MAX_HISTORY_SIZE = 1000  # Maximum number of queries to track


async def _call_gemini(prompt: str, max_retries: int = 3) -> dict | None:
    """Call the Google Gemini API to interpret a meme request with retry logic.
    
    Returns a dictionary with meme parameters like:
    {
      "template_id": "fry",
      "text": ["top text","bottom text"],
      "font": "thick",
      "style": "default"
    }
    
    Returns None if the API call fails or the response can't be parsed after retries.
    """
    api_key = getattr(settings, "GEMINI_API_KEY", None)
    model_name = getattr(settings, "GEMINI_MODEL", "gemini-pro")

    if not api_key:
        logger.error("GEMINI_API_KEY not configured")
        return None

    for attempt in range(max_retries):
        try:
            # Configure the Gemini client
            genai.configure(api_key=api_key)

            # Generate response
            if attempt > 0:
                logger.info(f"Retrying Gemini API call (attempt {attempt + 1}/{max_retries})")
                # Exponential backoff for rate limits
                await asyncio.sleep(2 ** attempt)
            else:
                logger.info(f"Calling Gemini API with model: {model_name}")
            
            model = genai.GenerativeModel(model_name)
            
            # Use coroutines for async operation
            response = await asyncio.to_thread(model.generate_content, prompt)
            
            if not response.text:
                logger.error("Empty response from Gemini")
                if attempt < max_retries - 1:
                    continue
                return None

            # Get the response text and parse it as JSON
            response_text = response.text
            logger.info(f"Raw response text: {response_text}")
            
            # Find JSON in response (handle cases where model outputs additional text)
            try:
                start = response_text.find('{')
                end = response_text.rfind('}') + 1
                if start >= 0 and end > start:
                    json_str = response_text[start:end]
                    result = json.loads(json_str)
                    logger.info(f"Parsed result: {result}")
                    return result
                else:
                    logger.error("No JSON object found in response")
                    if attempt < max_retries - 1:
                        continue
                    return None
            except json.JSONDecodeError as e:
                logger.error(f"Failed to parse response as JSON: {str(e)}")
                if attempt < max_retries - 1:
                    continue
                return None
                
        except Exception as e:
            error_str = str(e)
            # Check if it's a rate limit error (429)
            if "429" in error_str or "Resource exhausted" in error_str or "rate limit" in error_str.lower():
                if attempt < max_retries - 1:
                    wait_time = 2 ** (attempt + 2)  # Longer wait for rate limits: 4s, 8s, 16s
                    logger.warning(f"Rate limit hit, waiting {wait_time}s before retry {attempt + 2}/{max_retries}")
                    await asyncio.sleep(wait_time)
                    continue
                else:
                    logger.error(f"Gemini API rate limit error after {max_retries} attempts: {error_str}")
                    return None
            else:
                logger.error(f"Gemini API error: {error_str}")
                if attempt < max_retries - 1:
                    await asyncio.sleep(1)
                    continue
                return None
    
    return None


def _normalize_query(query: str) -> str:
    """Normalize query for tracking purposes (remove case, extra spaces, etc.)."""
    import re
    # Normalize: lowercase, remove extra spaces, remove punctuation variations
    normalized = re.sub(r'[^\w\s]', ' ', query.lower())
    normalized = re.sub(r'\s+', ' ', normalized).strip()
    return normalized


def _get_used_templates_for_query(query: str) -> list[str]:
    """Get list of templates recently used for this query."""
    normalized = _normalize_query(query)
    return _QUERY_TEMPLATE_HISTORY.get(normalized, [])


def _record_template_for_query(query: str, template_id: str):
    """Record that a template was used for a query."""
    normalized = _normalize_query(query)
    
    # Clean up old entries if cache is too large
    if len(_QUERY_TEMPLATE_HISTORY) > _MAX_HISTORY_SIZE:
        # Remove oldest entries (simple FIFO - remove first 20%)
        keys_to_remove = list(_QUERY_TEMPLATE_HISTORY.keys())[:_MAX_HISTORY_SIZE // 5]
        for key in keys_to_remove:
            del _QUERY_TEMPLATE_HISTORY[key]
    
    if normalized not in _QUERY_TEMPLATE_HISTORY:
        _QUERY_TEMPLATE_HISTORY[normalized] = []
    
    history = _QUERY_TEMPLATE_HISTORY[normalized]
    
    # Add to history if not already there (avoid duplicates)
    if template_id not in history:
        history.append(template_id)
    
    # Keep only last N templates
    if len(history) > _MAX_HISTORY_PER_QUERY:
        history.pop(0)


def _build_prompt(query: str) -> str:
    """Create a compact prompt describing available templates, fonts, filetypes, and expected output.
    List only valid templates (those with default.png, default.jpg, or default.gif).
    Now includes custom meme templates from "Meme templates" folder.
    """
    # Ensure custom templates are initialized
    try:
        ensure_custom_templates_initialized()
    except Exception as e:
        logger.warning(f"Could not initialize custom templates: {e}")
    
    templates = []
    extensions = set()
    try:
        for p in sorted(TEMPLATES_DIR.iterdir()):
            if p.is_dir():
                for ext in ("png", "jpg", "jpeg", "gif", "webp"):
                    if (p / f"default.{ext}").exists():
                        templates.append(p.name)
                        extensions.add(ext)
                        break
    except Exception:
        templates = []
        extensions = set()
    if not templates:
        templates = [p.name for p in sorted(TEMPLATES_DIR.iterdir()) if p.is_dir()]
    
    # Get custom templates and add to list
    try:
        custom_templates = get_custom_templates_list()
        # Filter out invalid template IDs (those starting with _custom- or not matching slug pattern)
        import re
        slug_pattern = re.compile(r'^[a-z0-9]+(?:-[a-z0-9]+)*$')
        valid_custom_templates = [ct for ct in custom_templates 
                                  if not ct.startswith('_custom-') and slug_pattern.match(ct)]
        # Add custom templates that aren't already in the list
        for ct in valid_custom_templates:
            if ct not in templates:
                templates.append(ct)
        logger.info(f"Added {len(valid_custom_templates)} custom templates to prompt (filtered {len(custom_templates) - len(valid_custom_templates)} invalid IDs)")
    except Exception as e:
        logger.warning(f"Could not load custom templates: {e}")
    
    # Always show all supported extensions to Gemini for variety, not just those found in templates
    extensions = {"png", "jpg", "gif", "webp"}

    fonts = []
    try:
        fonts_dir = Path(settings.ROOT) / "fonts"
        for f in sorted(fonts_dir.iterdir()):
            if f.suffix.lower() in {".ttf", ".ttc", ".otf"}:
                fonts.append(f.stem)
    except Exception:
        fonts = []
    
    # Get custom template metadata for better context with scenario matching
    custom_template_info = ""
    try:
        custom_templates_data = scan_custom_templates()
        if custom_templates_data:
            # Analyze query to find best matching templates by scenario
            query_lower = query.lower()
            query_words = set(query_lower.split())
            
            # Score templates by scenario relevance
            scored_templates = []
            for tid, info in custom_templates_data.items():
                score = 0
                template_name = info.get("name", tid).lower()
                template_tags = [t.lower() for t in info.get("tags", [])]
                
                # Check for direct name matches
                for word in query_words:
                    if len(word) > 3:  # Only check meaningful words
                        if word in template_name:
                            score += 3  # High score for name match
                        if any(word in tag for tag in template_tags):
                            score += 2  # Medium score for tag match
                        # Check for scenario keywords
                        if word in ["fire", "burning", "rescue", "save"] and any(kw in template_name for kw in ["fire", "rescue", "save"]):
                            score += 5
                        if word in ["angry", "mad", "furious"] and info.get("emotion") == "angry":
                            score += 4
                        if word in ["happy", "win", "success", "victory"] and info.get("emotion") == "happy":
                            score += 4
                        if word in ["sad", "cry", "lose", "oof"] and info.get("emotion") == "sad":
                            score += 4
                
                scored_templates.append((score, tid, info))
            
            # Sort by score and get top matches
            scored_templates.sort(key=lambda x: x[0], reverse=True)
            top_matches = scored_templates[:30]  # Top 30 scenario-relevant templates
            
            # Group by category
            reaction_templates = [(tid, info) for score, tid, info in top_matches 
                                 if info.get("category") == "reaction" and score > 0]
            general_templates = [(tid, info) for score, tid, info in top_matches 
                                if info.get("category") == "general" and score > 0]
            
            if reaction_templates or general_templates:
                custom_template_info = f"\n\nCUSTOM MEME TEMPLATES - SCENARIO MATCHED ({len(custom_templates_data)} total, showing top matches for your scenario):"
                
                if reaction_templates:
                    # Show template names with tags for better context
                    reaction_list = []
                    for tid, info in reaction_templates[:15]:  # Top 15 reaction matches
                        name = info.get("name", tid)
                        tags_preview = ", ".join(info.get("tags", [])[:3])
                        reaction_list.append(f"{tid} (\"{name}\" - tags: {tags_preview})")
                    custom_template_info += f"\n- Reaction memes (scenario-matched): {', '.join(reaction_list)}"
                    if len(reaction_templates) > 15:
                        custom_template_info += f" (and {len(reaction_templates) - 15} more reaction templates)"
                
                if general_templates:
                    general_list = []
                    for tid, info in general_templates[:15]:  # Top 15 general matches
                        name = info.get("name", tid)
                        tags_preview = ", ".join(info.get("tags", [])[:3])
                        general_list.append(f"{tid} (\"{name}\" - tags: {tags_preview})")
                    custom_template_info += f"\n- General memes (scenario-matched): {', '.join(general_list)}"
                    if len(general_templates) > 15:
                        custom_template_info += f" (and {len(general_templates) - 15} more general templates)"
                
                # Add scenario matching guidance
                custom_template_info += "\n\nSCENARIO MATCHING GUIDE:"
                custom_template_info += "\n- Template names in quotes show the actual meme scenario/context"
                custom_template_info += "\n- Tags indicate what scenarios/themes the template is good for"
                custom_template_info += "\n- Match the user's scenario description to template names/tags for best results"
                custom_template_info += "\n- Example: User says 'fire rescue' → use 'fire-rescue' template if available"
    except Exception as e:
        logger.warning(f"Could not get custom template info: {e}")

    # Get recently used templates for this query to avoid repetition
    used_templates = _get_used_templates_for_query(query)
    used_templates_info = ""
    if used_templates:
        used_templates_info = f"\n\n⚠️ IMPORTANT: Recently used templates for similar requests (AVOID THESE for variety): {', '.join(used_templates[:5])}"
        if len(used_templates) > 5:
            used_templates_info += f" (and {len(used_templates) - 5} more)"
        used_templates_info += "\n- Choose a DIFFERENT template from the available list to ensure variety"
    
    # Note: double braces {{ }} below escape literal JSON braces in an f-string
    prompt = f"""
You are an expert HUMOR meme generator specializing in creating funny, entertaining memes that make people laugh.
You have access to these real templates: {', '.join(templates[:150])}{'...' if len(templates) > 150 else ''}
{custom_template_info}{used_templates_info}
Available fonts: {', '.join(fonts)}.
File types: {', '.join(extensions)}.

═══════════════════════════════════════════════════════════════════════════════
CRITICAL RULES - ALWAYS FOLLOW THESE:
═══════════════════════════════════════════════════════════════════════════════

1. HUMOR IS MANDATORY - Your PRIMARY and ONLY goal is to create FUNNY, HUMOROUS, ENTERTAINING memes
   - Every meme MUST be funny, witty, or comedic - never serious or boring
   - Transform any request into a humorous meme, even if the original topic is serious
   - Use clever wordplay, irony, exaggeration, or absurdity to add humor
   - If user gives a serious topic, find the funny angle and make it hilarious
   - ABSOLUTELY NO abusive, offensive, sexual, or inappropriate content
   - Keep humor clean, family-friendly, and suitable for all audiences
   - Focus on relatable, clever, and lighthearted humor

2. HANDLE ALL KINDS OF REQUESTS:
   - Complete meme descriptions: "create a meme about X" → Generate full funny meme
   - Partial memes: "when you X" → Complete with funny bottom text
   - Single lines: "I love coding" → Complete into full humorous meme
   - Scenarios: "fire rescue situation" → Find matching template and create funny meme
   - Emotions: "angry programmer" → Use reaction template and add humor
   - Abstract concepts: "procrastination" → Find relatable template and make it funny
   - ANY input → ALWAYS generate a humorous meme, never return empty

3. TEMPLATE SELECTION - USE THE BEST MATCHING TEMPLATE:
   a) USER-SPECIFIED TEMPLATE: If user mentions a template name (e.g., "drake", "distracted-boyfriend"), USE IT EXACTLY
   b) SCENARIO MATCHING: Match user's scenario to template names/tags (shown in CUSTOM MEME TEMPLATES above)
   c) EMOTION MATCHING: For emotional queries, use reaction templates matching the emotion
   d) BEST FIT: Choose the template that best fits the humor context, not just any template
   e) VARIETY: If multiple templates fit, choose a different one than you might have used before
   f) FALLBACK: If no perfect match, use the closest template that can work humorously

4. COMPLETE PARTIAL MEMES:
   - If user gives only top text: Add a funny, relevant bottom text that completes the joke
   - If user gives only bottom text: Add a funny, relevant top text that sets up the punchline
   - If user gives a single line: Split into top/bottom and add complementary funny text
   - If user gives a scenario: Create both top and bottom text that make it funny
   - ALWAYS ensure the completed meme is humorous and makes sense

5. RESPECT USER SPECIFICATIONS (in priority order):
   a) Template name: If user says "use drake template" → use "drake" template
   b) Font: If user says "use Impact font" → use "Impact" font
   c) File format: If user says "make it a gif" → use "gif" extension
   d) Dimensions: If user specifies dimensions → note them (though you can't set them directly)
   e) Style: If user says "animated" → use animated style if available
   - If user specifies something, you MUST use it if available
   - If not available, use closest alternative but still generate the meme

6. FILE FORMAT VARIETY - ALWAYS USE DIFFERENT FORMATS:
   - NEVER default to "png" - rotate through all formats: png, jpg, gif, webp
   - If user doesn't specify format, pick randomly from: png, jpg, gif, webp
   - For similar requests, use DIFFERENT formats each time
   - Prefer variety: if last meme was png, use jpg or gif this time
   - Only use user-specified format if explicitly requested
   - Use hash-based selection to ensure different formats for same query

7. TEMPLATE VARIETY - CRITICAL FOR USER EXPERIENCE:
   - If templates are listed above as "recently used", AVOID THEM - choose a different one
   - Don't use the same template for similar requests - users want variety
   - Explore different templates that fit the scenario
   - Use the scenario-matched templates shown above for best results
   - Try different templates even for similar humor concepts
   - If user sends the same request multiple times, they want DIFFERENT templates each time
   - Prioritize templates that haven't been used recently for this query

═══════════════════════════════════════════════════════════════════════════════
TEMPLATE MATCHING STRATEGY (CRITICAL - FOLLOW IN ORDER):
═══════════════════════════════════════════════════════════════════════════════

1. EXACT TEMPLATE NAME MATCH: 
   - If user mentions a template name (e.g., "drake", "this-is-fine", "distracted-boyfriend"), 
     find the EXACT template ID from the list above and use it
   - Check both the template ID and the template name in quotes from scenario-matched templates

2. SCENARIO MATCH: 
   - Use the scenario-matched templates shown above (they're pre-filtered for relevance)
   - Match keywords from user's request to template names and tags
   - Example: "fire rescue" → use "fire-rescue-feu-incendie-choisir-sauver" if available

3. EMOTION/CATEGORY MATCH: 
   - For emotional queries (angry, happy, sad, etc.), use reaction templates matching that emotion
   - For general humor, use general templates

4. KEYWORD MATCH: 
   - Find templates whose tags match keywords in the user's request
   - Prioritize templates with multiple matching keywords

5. BEST FIT: 
   - Choose the template that best supports the humor you're creating
   - Consider the meme format (pointing, reaction, comparison, etc.)

6. VARIETY: 
   - If multiple templates fit equally well, choose a different one than you might have used before
   - Avoid repetition for similar requests

CRITICAL: ALWAYS use a template from the lists above. NEVER invent template names.

═══════════════════════════════════════════════════════════════════════════════
OUTPUT FORMAT:
═══════════════════════════════════════════════════════════════════════════════

Return ONLY valid JSON (no markdown, no code blocks, no extra text):
{{
    "template_id": "template_name",
    "text": ["top text", "bottom text"],
    "font": "font_name",
    "extension": "filetype",
    "style": "default"
}}

REQUIREMENTS:
- template_id: MUST be an exact match from the template list above (check scenario-matched templates first)
  * AVOID templates listed in "recently used" section above - choose a different one for variety
- text: MUST be an array with 2 elements (top and bottom) - both MUST be funny/humorous/entertaining
  * Text MUST be clean, family-friendly, and appropriate for all audiences
  * NO abusive, offensive, sexual, or inappropriate content
  * Focus on clever wordplay, relatable situations, and lighthearted humor
- font: Must match a font from the list above (or omit if not specified)
- extension: Must be one of: png, jpg, gif, webp (rotate for variety, don't default to png)
  * Use different format than might have been used before for similar queries
- style: Usually "default" unless user requests "animated" or template has specific styles

CRITICAL: 
- The "text" array MUST contain funny, humorous, entertaining content that makes people laugh
- The template_id MUST exist in the template lists above
- Always prioritize humor and entertainment value
- ABSOLUTELY NO abusive, sexual, or offensive content - keep it clean and funny
- If user's request could be interpreted inappropriately, find the clean, funny angle instead

═══════════════════════════════════════════════════════════════════════════════
USER REQUEST: {query}
═══════════════════════════════════════════════════════════════════════════════

Analyze the request carefully:
1. Identify if user specified a template name → use it exactly (unless it was recently used for this query)
2. Identify the scenario/emotion → use scenario-matched templates from above
3. Generate FUNNY, HUMOROUS, CLEAN text that makes the meme entertaining (NO abuse/sexual content)
4. Choose the BEST matching template from the lists above (AVOID recently used templates for variety)
5. Complete any partial input into a full humorous meme
6. Select a different file format than might have been used before (rotate: png, jpg, gif, webp)

IMPORTANT: 
- If this is a repeat request, choose a DIFFERENT template and format than before
- Keep all content clean, family-friendly, and appropriate
- Make it genuinely funny and entertaining

Return ONLY the JSON object, nothing else.
"""
    return prompt


async def interpret_and_build_url(request, query: str) -> dict | None:
    """Interpret a natural-language query via Gemini and build a memegen URL.

    Always generate a meme if at all possible, using the closest valid template if directly requested one isn't available.
    Retries Gemini API calls on rate limits to ensure we always get a Gemini-generated meme.
    Ensures template and format variety for repeated queries.
    """
    # Declare global variables at the start of the function
    global _LAST_TEMPLATE_ID, _LAST_EXTENSION
    
    prompt = _build_prompt(query)
    data = await _call_gemini(prompt, max_retries=5)  # More retries for reliability
    
    # If Gemini fails after retries, try one more time with a shorter prompt
    if not data:
        logger.warning("Gemini API failed after retries, attempting with simplified prompt")
        
        # Get a list of available templates for simplified prompt
        try:
            templates_dir = Path(settings.ROOT) / "templates"
            available_template_list = [p.name for p in sorted(templates_dir.iterdir()) 
                                     if p.is_dir() and any((p / f"default.{ext}").exists() 
                                                          for ext in ("png", "jpg", "jpeg", "gif", "webp"))][:100]
        except Exception:
            available_template_list = []
        
        # Create a shorter, more focused prompt
        simplified_prompt = f"""You are a humor meme generator. Generate a FUNNY meme based on: "{query}"

Available templates: {', '.join(available_template_list) if available_template_list else 'drake, fine, fry, aag, distracted-boyfriend'}

Return JSON only (no markdown):
{{
    "template_id": "best_matching_template_from_list",
    "text": ["funny top text", "funny bottom text"],
    "font": "Impact",
    "extension": "jpg",
    "style": "default"
}}

CRITICAL: Make it HILARIOUS and use a template from the list above."""
        
        data = await _call_gemini(simplified_prompt, max_retries=3)
        
        if not data:
            logger.error("Gemini API failed completely after all retries - cannot generate meme")
            return None

    # Merge returned values conservatively
    template_id = data.get("template_id") or data.get("template")
    text = data.get("text") or data.get("lines") or []
    if isinstance(text, str):
        text = [text]
    font = data.get("font") or ""
    style = data.get("style") or "default"
    extension = data.get("extension") or ""
    image_url = data.get("image_url") or data.get("background")
    
    # Check if user explicitly requested a font in the query
    user_requested_font = None
    query_lower = query.lower()
    try:
        fonts_dir = Path(settings.ROOT) / "fonts"
        available_fonts = [f.stem for f in fonts_dir.iterdir() if f.suffix.lower() in {".ttf", ".ttc", ".otf"}]
        for f in available_fonts:
            if f.lower() in query_lower or f"font {f}" in query_lower or f"use {f}" in query_lower:
                user_requested_font = f
                break
    except Exception:
        pass
    
    # If user requested a font, use it
    if user_requested_font:
        font = user_requested_font
        logger.info(f"User requested font: {user_requested_font}")
    
    # Validate template_id - must match slug pattern
    import re
    slug_pattern = re.compile(r'^[a-z0-9]+(?:-[a-z0-9]+)*$')
    if template_id and (not slug_pattern.match(template_id) or template_id.startswith('_custom-')):
        logger.warning(f"Gemini returned invalid template_id: {template_id}, will try to find valid alternative")
        # Try to find a valid template that matches the query
        template_id = None  # Will be handled by fallback logic below
    
    logger.info(f"Gemini returned extension: {extension} for query")
    
    # Check if user explicitly requested a format in the query
    user_requested_format = None
    query_lower = query.lower()
    for fmt in ["png", "jpg", "jpeg", "gif", "webp"]:
        if fmt in query_lower or f".{fmt}" in query_lower:
            user_requested_format = fmt
            break
    
    # If user requested a format, use it; otherwise use Gemini's choice or fallback to variety logic
    if user_requested_format:
        extension = user_requested_format
    elif extension and extension.lower() in settings.ALLOWED_EXTENSIONS:
        extension = extension.lower()
    else:
        extension = ""  # Will use variety logic

    allowed_templates = []
    try:
        from pathlib import Path
        import re
        slug_pattern = re.compile(r'^[a-z0-9]+(?:-[a-z0-9]+)*$')
        templates_dir = Path(settings.ROOT) / "templates"
        for p in sorted(templates_dir.iterdir()):
            if (p.is_dir() and 
                not p.name.startswith('_custom-') and  # Exclude _custom- prefixed IDs
                slug_pattern.match(p.name) and  # Must match slug pattern
                any((p / f"default.{ext}").exists() for ext in ("png", "jpg", "jpeg", "gif", "webp"))):
                allowed_templates.append(p.name)
    except Exception:
        allowed_templates = []

    used_fallback = False
    original_template = template_id
    
    # Check if user explicitly requested a template name in the query
    user_requested_template = None
    query_lower = query.lower()
    for t in allowed_templates:
        # Check for exact template name match in query
        if (t.lower() in query_lower or 
            f"template {t}" in query_lower or 
            f"use {t}" in query_lower or
            f"{t} template" in query_lower or
            f"{t} meme" in query_lower):
            user_requested_template = t
            break
    
    # If user requested a specific template, prioritize it
    if user_requested_template and user_requested_template in allowed_templates:
        template_id = user_requested_template
        logger.info(f"User requested template: {user_requested_template}")
    elif template_id and (template_id not in allowed_templates):
        # Smart template matching: try to find best match based on query context
        logger.warning(f"Gemini returned template '{template_id}' not in allowed templates, finding best match")
        
        # Get recently used templates for this query to avoid repetition
        used_templates_for_query = _get_used_templates_for_query(query)
        
        # First, try to find scenario-matched templates from custom templates
        best_match = None
        try:
            custom_templates_data = scan_custom_templates()
            query_words = set(query_lower.split())
            
            # Score templates by relevance to query, penalizing recently used ones
            scored_templates = []
            for tid, info in custom_templates_data.items():
                if tid not in allowed_templates:
                    continue
                score = 0
                template_name = info.get("name", tid).lower()
                template_tags = [t.lower() for t in info.get("tags", [])]
                
                # Penalize recently used templates for this query
                if tid in used_templates_for_query:
                    score -= 20  # Strong penalty for recently used
                
                # Check for exact template_id match (partial)
                if template_id.lower() in tid.lower() or tid.lower() in template_id.lower():
                    score += 10
                
                # Check for name match
                for word in query_words:
                    if len(word) > 3:
                        if word in template_name:
                            score += 5
                        if any(word in tag for tag in template_tags):
                            score += 3
                        if word in tid.lower():
                            score += 2
                
                if score > 0:
                    scored_templates.append((score, tid))
            
            if scored_templates:
                scored_templates.sort(key=lambda x: x[0], reverse=True)
                # Prefer templates that haven't been used recently
                for score, tid in scored_templates:
                    if tid not in used_templates_for_query:
                        best_match = tid
                        logger.info(f"Found scenario-matched template (unused): {best_match} (score: {score})")
                        break
                if not best_match:
                    best_match = scored_templates[0][1]
                    logger.info(f"Found scenario-matched template (all used, using best): {best_match} (score: {scored_templates[0][0]})")
        except Exception as e:
            logger.warning(f"Error in scenario matching: {e}")
        
        # If no scenario match, try string similarity
        if not best_match:
            import difflib
            matches = difflib.get_close_matches(template_id, allowed_templates, n=10, cutoff=0.3)
            if matches:
                # Prefer matches that haven't been used recently for this query or globally
                for match in matches:
                    if match != _LAST_TEMPLATE_ID and match not in used_templates_for_query:
                        best_match = match
                        break
                # If all matches were used, try to find one not in recent history
                if not best_match:
                    for match in matches:
                        if match not in used_templates_for_query:
                            best_match = match
                            break
                # Last resort: use best match even if used
                if not best_match:
                    best_match = matches[0]
        
        if best_match:
            template_id = best_match
            used_fallback = True
            logger.info(f"Using best match template: {template_id}")
        elif allowed_templates:
            # Last resort: pick a random template different from last used
            alts = [t for t in allowed_templates if t != _LAST_TEMPLATE_ID] if _LAST_TEMPLATE_ID else allowed_templates
            template_id = random.choice(alts) if alts else allowed_templates[0]
            used_fallback = True
            logger.warning(f"No good match found, using random template: {template_id}")
        else:
            logger.error("No allowed templates available")
            return None

    # Helper to choose a valid extension when not specified, varying based on context
    def _choose_extension(meme_template_id: str = None, meme_text: list = None, prefer_animated: bool = False, user_requested: str = None, query: str = None) -> str:
        """Choose extension with maximum variety - never default to png unless user requests it."""
        # If user explicitly requested a format, use it
        if user_requested and user_requested.lower() in settings.ALLOWED_EXTENSIONS:
            return user_requested.lower()
        
        static_exts = sorted(list(settings.ALLOWED_EXTENSIONS - settings.ANIMATED_EXTENSIONS))
        animated_exts = sorted(list(settings.ANIMATED_EXTENSIONS & settings.ALLOWED_EXTENSIONS))
        population = animated_exts if prefer_animated and animated_exts else static_exts or list(settings.ALLOWED_EXTENSIONS)
        
        # Ensure we have variety - prefer formats other than png
        if len(population) > 1:
            # Remove png from priority list to encourage variety
            variety_population = [e for e in population if e != "png"] or population
        else:
            variety_population = population
        
        # Use context-based selection for variety, including query for same-query variety
        if query:
            # Include query in hash to get different formats for same query
            context_str = _normalize_query(query) + str(meme_template_id or "") + "".join(meme_text[:2] if meme_text else [])
            context_hash = abs(hash(context_str))
        elif meme_template_id and meme_text:
            context_str = str(meme_template_id) + "".join(meme_text[:2] if meme_text else [])
            context_hash = abs(hash(context_str))
        else:
            context_hash = random.randint(0, 10000)
        
        # Access and update global _LAST_EXTENSION
        global _LAST_EXTENSION
        try:
            last = _LAST_EXTENSION
        except NameError:
            last = None
        
        # Prefer different from last, and prefer non-png formats
        choices = [e for e in variety_population if e != last] or variety_population
        
        # Use hash-based selection for deterministic but varied results
        selection_index = context_hash % len(choices)
        chosen = choices[selection_index] if choices else variety_population[0] if variety_population else population[0] if population else "jpg"
        
        _LAST_EXTENSION = chosen
        return chosen

    # Build a memegen URL using existing model utilities
    if image_url:
        # Treat as custom background
        template = models.Template.objects.get_or_create(image_url)
        if not extension:
            extension = _choose_extension(template_id, text, style == "animated", user_requested_format, query)
        url = template.build_custom_url(request, text, background=image_url, style=style, font=font, extension=extension)
    elif template_id:
        template = models.Template.objects.get_or_create(template_id)
        # Validate requested style; if invalid, fall back to default
        try:
            styles = template.styles
        except Exception:
            styles = []
        if style and style not in {"default", "animated"} and style not in styles:
            style = "default"
        # Avoid reusing the same template for this query (unless user specifically requested it)
        used_templates_for_query = _get_used_templates_for_query(query)
        if (not user_requested_template and 
            template_id in used_templates_for_query and 
            allowed_templates):
            # Find alternatives that haven't been used for this query
            alts = [t for t in allowed_templates if t != template_id and t not in used_templates_for_query]
            if not alts:
                # If all templates were used, try to find ones not used recently
                alts = [t for t in allowed_templates if t != template_id]
            
            if alts:
                # Prefer templates that match the scenario better
                import difflib
                # Try to find similar templates that might work better
                similar = difflib.get_close_matches(original_template or template_id, alts, n=5)
                if similar:
                    # Prefer ones not in used history
                    for alt in similar:
                        if alt not in used_templates_for_query:
                            template_id = alt
                            break
                    if template_id == (original_template or template_id):
                        template_id = similar[0]
                else:
                    # Random selection from alternatives
                    template_id = random.choice(alts)
                template = models.Template.objects.get_or_create(template_id)
                logger.info(f"Switched to different template for query variety: {template_id} (was in history: {used_templates_for_query[:3]})")
        # Also avoid global repetition
        elif (not user_requested_template and 
              _LAST_TEMPLATE_ID and 
              template_id == _LAST_TEMPLATE_ID and 
              allowed_templates):
            alts = [t for t in allowed_templates if t != template_id and t not in used_templates_for_query]
            if not alts:
                alts = [t for t in allowed_templates if t != template_id]
            if alts:
                template_id = random.choice(alts)
                template = models.Template.objects.get_or_create(template_id)
                logger.info(f"Switched to different template for global variety: {template_id}")
        if not extension:
            extension = _choose_extension(template_id, text, style == "animated", user_requested_format, query)
        url = template.build_custom_url(request, text, style=style, font=font, extension=extension)
        if not template.valid:
            # Try fallback template, if not already tried
            import difflib
            valid_templates = [t for t in allowed_templates if t != template_id]
            fallback = difflib.get_close_matches(template_id, valid_templates, n=1)
            if fallback:
                template_id = fallback[0]
                template = models.Template.objects.get_or_create(template_id)
                if not extension:
                    extension = _choose_extension(template_id, text, style == "animated", user_requested_format, query)
                url = template.build_custom_url(request, text, style=style, font=font, extension=extension)
                used_fallback = True
            elif valid_templates:
                # Pick a random valid template different from last used
                alts = [t for t in valid_templates if t != _LAST_TEMPLATE_ID] if _LAST_TEMPLATE_ID else valid_templates
                template_id = random.choice(alts) if alts else valid_templates[0]
                template = models.Template.objects.get_or_create(template_id)
                if not extension:
                    extension = _choose_extension(template_id, text, style == "animated", user_requested_format, query)
                url = template.build_custom_url(request, text, style=style, font=font, extension=extension)
                used_fallback = True
            else:
                return None
    else:
        # Nothing usable returned
        return None

    url, _updated = await utils.meta.tokenize(request, url)
    _LAST_TEMPLATE_ID = template_id or _LAST_TEMPLATE_ID
    
    # Record template usage for this query to ensure variety on repeat requests
    if template_id:
        _record_template_for_query(query, template_id)
        logger.info(f"Recorded template '{template_id}' for query (history now: {_get_used_templates_for_query(query)[-3:]})")
    
    if used_fallback:
        logger.warning(f"Gemini requested invalid template '{original_template}', using fallback '{template_id}' instead.")
    return {"url": url, "generator": "gemini", "confidence": float(data.get("confidence", 0.75))}
