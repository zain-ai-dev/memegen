"""
Custom Meme Templates Loader

Scans the "Meme templates" and "Reaction meme templates" folders
and integrates them into the template system.
"""
import os
import shutil
from pathlib import Path
from typing import Dict, List, Optional
from sanic.log import logger

from .. import settings

# Try to import yaml - it's available through datafiles dependency
try:
    import yaml
except ImportError:
    try:
        from ruamel import yaml
    except ImportError:
        yaml = None
        logger.warning("YAML library not available - custom template config generation will be limited")


CUSTOM_TEMPLATES_DIR = Path(settings.ROOT) / "Meme templates"
REACTION_TEMPLATES_DIR = CUSTOM_TEMPLATES_DIR / "Reaction meme templates" / "Reactions"


def scan_custom_templates() -> Dict[str, Dict]:
    """
    Scan custom meme templates folders and return template metadata.
    
    Returns:
        Dict mapping template_id to template info:
        {
            "template_id": {
                "path": Path to image file,
                "category": "reaction" | "general",
                "emotion": "angry" | "happy" | "sad" | etc.,
                "tags": list of tags extracted from filename/folder,
                "name": display name
            }
        }
    """
    templates = {}
    
    # Scan main "Meme templates" folder
    if CUSTOM_TEMPLATES_DIR.exists():
        for image_file in CUSTOM_TEMPLATES_DIR.iterdir():
            if image_file.is_file() and image_file.suffix.lower() in {".jpg", ".jpeg", ".png", ".gif", ".webp"}:
                template_id = _generate_template_id(image_file.stem)
                tags = _extract_tags_from_filename(image_file.stem)
                
                templates[template_id] = {
                    "path": image_file,
                    "category": "general",
                    "emotion": None,
                    "tags": tags + ["meme", "humor", "funny"],
                    "name": _clean_name(image_file.stem),
                    "source": None  # Don't set source to avoid layout issues
                }
    
    # Scan "Reaction meme templates" folder
    if REACTION_TEMPLATES_DIR.exists():
        for emotion_folder in REACTION_TEMPLATES_DIR.iterdir():
            if emotion_folder.is_dir():
                emotion = _extract_emotion(emotion_folder.name)
                
                for image_file in emotion_folder.iterdir():
                    if image_file.is_file() and image_file.suffix.lower() in {".jpg", ".jpeg", ".png", ".gif", ".webp"}:
                        template_id = _generate_template_id(f"{emotion_folder.name}_{image_file.stem}")
                        tags = _extract_tags_from_filename(image_file.stem)
                        tags.extend(_extract_emotion_tags(emotion))
                        
                        templates[template_id] = {
                            "path": image_file,
                            "category": "reaction",
                            "emotion": emotion,
                            "tags": tags + ["reaction", "meme", "humor", "funny"],
                            "name": _clean_name(image_file.stem),
                            "source": None,  # Don't set source to avoid layout issues - use category/tags instead
                            "emotion_folder": emotion_folder.name
                        }
    
    logger.info(f"Scanned {len(templates)} custom meme templates")
    return templates


def _generate_template_id(filename: str) -> str:
    """Generate a valid template ID from filename that matches slug pattern: ^[a-z0-9]+(?:-[a-z0-9]+)*$"""
    import re
    
    # Remove special characters, keep alphanumeric and dashes
    template_id = re.sub(r'[^a-z0-9-]', '-', filename.lower())
    # Remove multiple dashes
    template_id = re.sub(r'-+', '-', template_id)
    # Remove leading/trailing dashes (critical for slug pattern)
    template_id = template_id.strip('-')
    
    # Ensure it doesn't start or end with dash, and has at least one alphanumeric char
    # Slug pattern: ^[a-z0-9]+(?:-[a-z0-9]+)*$ - must start with alphanumeric, can have dashes in middle
    if not template_id:
        template_id = 'meme-template'
    elif not template_id[0].isalnum():
        # If doesn't start with alphanumeric, add prefix
        template_id = 'meme-' + template_id
    
    # Remove any trailing dashes again (in case prefix added dash)
    template_id = template_id.rstrip('-')
    
    # Ensure it ends with alphanumeric (remove trailing dashes)
    while template_id and not template_id[-1].isalnum():
        template_id = template_id[:-1]
    
    # If somehow empty after cleanup, use default
    if not template_id:
        template_id = 'meme-template'
    
    # Limit length but ensure it doesn't end with dash
    if len(template_id) > 50:
        template_id = template_id[:50].rstrip('-')
        # Ensure it still ends with alphanumeric after truncation
        while template_id and not template_id[-1].isalnum():
            template_id = template_id[:-1]
        if not template_id:
            template_id = 'meme-template'
    
    # Final validation - ensure it matches slug pattern exactly
    if not re.match(r'^[a-z0-9]+(?:-[a-z0-9]+)*$', template_id):
        # Fallback: create valid slug by removing all non-alphanumeric
        template_id = re.sub(r'[^a-z0-9]', '', filename.lower()[:50])
        if not template_id:
            template_id = 'meme-template'
        # If still doesn't match, add prefix
        if not re.match(r'^[a-z0-9]+(?:-[a-z0-9]+)*$', template_id):
            template_id = 'meme-' + template_id if template_id else 'meme-template'
    
    # Final check - ensure no trailing dash
    template_id = template_id.rstrip('-')
    if not template_id:
        template_id = 'meme-template'
    
    return template_id


def _extract_tags_from_filename(filename: str) -> List[str]:
    """Extract tags from filename by splitting on common separators."""
    import re
    # Replace common separators with spaces
    text = re.sub(r'[_\-\s\.]+', ' ', filename.lower())
    # Split into words
    words = [w for w in text.split() if len(w) > 2 and w.isalpha()]
    
    # Add scenario-specific keywords based on common meme scenarios
    scenario_keywords = {
        "fire": ["fire", "burning", "rescue", "save", "emergency", "flame", "burn"],
        "rescue": ["rescue", "save", "help", "emergency", "saving", "saved"],
        "angry": ["angry", "mad", "furious", "rage", "annoyed", "upset", "irritated"],
        "programmer": ["programmer", "coding", "debug", "code", "developer", "software", "programming", "script"],
        "distracted": ["distracted", "boyfriend", "choice", "decision", "choosing", "pick"],
        "drake": ["drake", "pointing", "approving", "disapproving", "dismissing"],
        "fine": ["fine", "okay", "accepting", "this is fine", "everything is fine"],
        "spiderman": ["spiderman", "pointing", "same", "spider-man"],
        "brain": ["brain", "expanding", "smart", "intelligence", "thinking", "mind"],
        "coffee": ["coffee", "caffeine", "morning", "wake up"],
        "work": ["work", "office", "job", "boss", "meeting", "deadline"],
        "sleep": ["sleep", "tired", "bed", "wake up", "morning"],
        "food": ["food", "eat", "hungry", "pizza", "burger", "taco"],
        "money": ["money", "rich", "poor", "buy", "spend", "expensive"],
        "love": ["love", "relationship", "dating", "girlfriend", "boyfriend"],
        "school": ["school", "homework", "exam", "test", "study", "student"],
        "gaming": ["game", "gaming", "play", "player", "win", "lose"],
        "internet": ["internet", "wifi", "connection", "online", "offline"],
        "phone": ["phone", "smartphone", "text", "call", "message"],
        "social": ["social", "media", "facebook", "instagram", "twitter"],
    }
    
    # Check if filename contains scenario keywords and add related tags
    for scenario, keywords in scenario_keywords.items():
        if any(kw in text for kw in keywords):
            words.extend([scenario] + keywords[:2])
            break
    
    # Limit to 15 words to include scenario context
    return list(dict.fromkeys(words))[:15]  # Remove duplicates while preserving order


def _extract_emotion(folder_name: str) -> str:
    """Extract emotion from folder name."""
    folder_lower = folder_name.lower()
    
    if "angry" in folder_lower or "wicked" in folder_lower:
        return "angry"
    elif "yes" in folder_lower or "win" in folder_lower or "love" in folder_lower:
        return "happy"
    elif "sad" in folder_lower or "oof" in folder_lower or "lose" in folder_lower:
        return "sad"
    elif "dumb" in folder_lower or "genius" in folder_lower:
        return "confused"
    elif "humm" in folder_lower or "boring" in folder_lower or "not interesting" in folder_lower:
        return "bored"
    elif "offend" in folder_lower:
        return "offended"
    else:
        return "neutral"


def _extract_emotion_tags(emotion: str) -> List[str]:
    """Get tags based on emotion."""
    emotion_tags = {
        "angry": ["angry", "mad", "furious", "rage", "wicked"],
        "happy": ["happy", "win", "victory", "success", "love", "yes", "celebration"],
        "sad": ["sad", "crying", "depressed", "lose", "oof", "defeat", "failure"],
        "confused": ["confused", "dumb", "genius", "thinking", "question"],
        "bored": ["bored", "boring", "not interesting", "humm", "meh"],
        "offended": ["offended", "insulted", "hurt", "upset"]
    }
    return emotion_tags.get(emotion, [])


def _clean_name(filename: str) -> str:
    """Clean filename for display."""
    import re
    # Replace underscores and dashes with spaces
    name = re.sub(r'[_\-\s]+', ' ', filename)
    # Capitalize words
    return ' '.join(word.capitalize() for word in name.split())


def get_custom_templates_list() -> List[str]:
    """Get list of custom template IDs for AI prompt."""
    templates = scan_custom_templates()
    return list(templates.keys())


def get_custom_templates_by_emotion(emotion: str = None) -> Dict[str, Dict]:
    """Get custom templates filtered by emotion."""
    all_templates = scan_custom_templates()
    if emotion:
        return {tid: info for tid, info in all_templates.items() 
                if info.get("emotion") == emotion}
    return all_templates


def get_custom_templates_by_tags(query_tags: List[str]) -> Dict[str, Dict]:
    """Get custom templates that match any of the query tags."""
    all_templates = scan_custom_templates()
    query_tags_lower = [t.lower() for t in query_tags]
    
    matching = {}
    for tid, info in all_templates.items():
        template_tags = [t.lower() for t in info.get("tags", [])]
        if any(qt in template_tags or any(qt in tag for tag in template_tags) 
               for qt in query_tags_lower):
            matching[tid] = info
    
    return matching


def initialize_custom_template(template_id: str, template_info: Dict) -> bool:
    """
    Initialize a custom template by creating its directory structure and config.yml.
    Returns True if successful, False otherwise.
    """
    try:
        templates_dir = Path(settings.ROOT) / "templates"
        template_dir = templates_dir / template_id
        template_dir.mkdir(parents=True, exist_ok=True)
        
        # Copy image file
        source_image = template_info["path"]
        # Determine extension
        ext = source_image.suffix.lower()
        if ext not in {".jpg", ".jpeg", ".png", ".gif", ".webp"}:
            ext = ".jpg"  # default
        
        dest_image = template_dir / f"default{ext}"
        if not dest_image.exists() or settings.DEBUG:
            shutil.copy2(source_image, dest_image)
            logger.info(f"Copied custom template image: {template_id}")
        
        # Create config.yml
        config_path = template_dir / "config.yml"
        if not config_path.exists() or settings.DEBUG:
            config_data = {
                "name": template_info.get("name", template_id),
                "source": None,  # Explicitly set to None to avoid layout/style issues
                # The layout property uses source, and setting it to "reaction" or "custom" 
                # causes "default.reaction" or "default.custom" style issues
                "keywords": template_info.get("tags", [])[:10],  # Limit keywords
                "text": [
                    {
                        "style": "upper",
                        "color": "white",
                        "font": "thick",
                        "anchor_x": 0.0,
                        "anchor_y": 0.0,
                        "angle": 0.0,
                        "scale_x": 1.0,
                        "scale_y": 0.2,
                        "align": "center",
                        "start": 0.0,
                        "stop": 1.0
                    },
                    {
                        "style": "upper",
                        "color": "white",
                        "font": "thick",
                        "anchor_x": 0.0,
                        "anchor_y": 0.8,
                        "angle": 0.0,
                        "scale_x": 1.0,
                        "scale_y": 0.2,
                        "align": "center",
                        "start": 0.0,
                        "stop": 1.0
                    }
                ],
                "example": ["Top Text", "Bottom Text"],
                "overlay": [
                    {
                        "center_x": 0.5,
                        "center_y": 0.5,
                        "angle": 0.0,
                        "scale": 0.25
                    }
                ]
            }
            
            if yaml:
                with open(config_path, 'w', encoding='utf-8') as f:
                    # Remove source from config_data if it's None to avoid YAML writing "null"
                    config_to_write = {k: v for k, v in config_data.items() if v is not None}
                    yaml.dump(config_to_write, f, default_flow_style=False, allow_unicode=True)
            else:
                # Fallback: create minimal config without yaml
                logger.warning(f"YAML not available, creating minimal config for {template_id}")
                with open(config_path, 'w', encoding='utf-8') as f:
                    f.write(f"name: {template_info.get('name', template_id)}\n")
                    # Don't write source field to avoid layout/style issues
                    f.write("keywords: []\n")
                    f.write("text:\n")
                    f.write("  - style: upper\n")
                    f.write("    color: white\n")
                    f.write("    font: thick\n")
                    f.write("    anchor_x: 0.0\n")
                    f.write("    anchor_y: 0.0\n")
                    f.write("  - style: upper\n")
                    f.write("    color: white\n")
                    f.write("    font: thick\n")
                    f.write("    anchor_x: 0.0\n")
                    f.write("    anchor_y: 0.8\n")
                    f.write("example:\n")
                    f.write("  - Top Text\n")
                    f.write("  - Bottom Text\n")
                    f.write("overlay:\n")
                    f.write("  - center_x: 0.5\n")
                    f.write("    center_y: 0.5\n")
            
            logger.info(f"Created config.yml for custom template: {template_id}")
        
        return True
    except Exception as e:
        logger.error(f"Error initializing custom template {template_id}: {e}")
        return False


def cleanup_existing_templates():
    """
    Clean up existing templates with invalid IDs or source values.
    Fixes:
    1. Templates with IDs ending in dashes
    2. Templates with source set to "reaction", "custom", or "general"
    """
    import re
    from pathlib import Path
    
    templates_dir = Path(settings.ROOT) / "templates"
    if not templates_dir.exists():
        return
    
    slug_pattern = re.compile(r'^[a-z0-9]+(?:-[a-z0-9]+)*$')
    fixed_count = 0
    
    for template_dir in templates_dir.iterdir():
        if not template_dir.is_dir():
            continue
        
        template_id = template_dir.name
        config_path = template_dir / "config.yml"
        
        # Check if template ID is invalid
        if not slug_pattern.match(template_id):
            logger.warning(f"Found template with invalid ID: {template_id}")
            # Try to fix the ID by regenerating it
            fixed_id = _generate_template_id(template_id)
            if fixed_id != template_id and slug_pattern.match(fixed_id):
                # Rename the directory
                new_dir = templates_dir / fixed_id
                if not new_dir.exists():
                    try:
                        template_dir.rename(new_dir)
                        logger.info(f"Renamed template directory: {template_id} -> {fixed_id}")
                        fixed_count += 1
                        template_dir = new_dir
                        template_id = fixed_id
                        config_path = template_dir / "config.yml"
                    except Exception as e:
                        logger.error(f"Failed to rename template {template_id}: {e}")
                        continue
        
        # Fix config.yml if source is set to category name
        if config_path.exists() and yaml:
            try:
                with open(config_path, 'r', encoding='utf-8') as f:
                    config_data = yaml.safe_load(f) or {}
                
                source = config_data.get("source", "")
                if source and isinstance(source, str) and source.lower().strip() in {"reaction", "custom", "general"}:
                    # Remove source field or set to None
                    config_data["source"] = None
                    with open(config_path, 'w', encoding='utf-8') as f:
                        config_to_write = {k: v for k, v in config_data.items() if v is not None}
                        yaml.dump(config_to_write, f, default_flow_style=False, allow_unicode=True)
                    logger.info(f"Fixed source field in config.yml for template: {template_id}")
                    fixed_count += 1
            except Exception as e:
                logger.warning(f"Could not fix config.yml for {template_id}: {e}")
    
    if fixed_count > 0:
        logger.info(f"Cleaned up {fixed_count} existing templates")


def ensure_custom_templates_initialized() -> Dict[str, Dict]:
    """
    Ensure all custom templates are initialized in the templates folder.
    Returns dict of initialized templates.
    """
    # First, clean up existing templates
    cleanup_existing_templates()
    
    custom_templates = scan_custom_templates()
    initialized = {}
    
    for template_id, template_info in custom_templates.items():
        if initialize_custom_template(template_id, template_info):
            initialized[template_id] = template_info
    
    logger.info(f"Initialized {len(initialized)} custom templates")
    return initialized

