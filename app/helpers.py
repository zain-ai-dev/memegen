from sanic import Request
from sanic.log import logger

from . import settings, utils
from .models import Template


def get_valid_templates(
    request: Request, query: str = "", animated: bool | None = None
) -> list[dict]:
    templates: list[Template] = Template.objects.filter(valid=True, _exclude="_custom")
    if query:
        templates = [t for t in templates if t.matches(query)]
    else:
        templates = sorted(templates)
    if animated is True:
        templates = [t for t in templates if "animated" in t.styles]
    elif animated is False:
        templates = [t for t in templates if "animated" not in t.styles]
    
    # Filter out templates with invalid IDs (those ending with dashes or not matching slug pattern)
    import re
    slug_pattern = re.compile(r'^[a-z0-9]+(?:-[a-z0-9]+)*$')
    valid_templates = []
    for template in templates:
        if slug_pattern.match(template.id):
            try:
                valid_templates.append(template.jsonify(request))
            except Exception as e:
                # Skip templates that cause errors (e.g., invalid template_id for URL building)
                logger.warning(f"Skipping template {template.id} due to error: {e}")
                continue
        else:
            logger.warning(f"Skipping template with invalid ID (doesn't match slug pattern): {template.id}")
    
    return valid_templates


def get_example_images(
    request: Request, query: str = "", *, animated: bool | None = None
) -> list[tuple[str, str]]:
    templates = Template.objects.filter(valid=True, _exclude="_custom")
    if query:
        templates = [t for t in templates if t.matches(query)]
    else:
        templates = sorted(templates)

    if animated is None:
        animated = utils.urls.flag(request, "animated")
        exact = True
    else:
        exact = False

    images = []
    for template in templates:
        if exact and animated is True and "animated" not in template.styles:
            continue
        if exact and animated is False and "animated" in template.styles:
            continue

        if animated is True:
            extension = settings.DEFAULT_ANIMATED_EXTENSION
        elif "animated" in template.styles and animated is not False:
            extension = settings.DEFAULT_ANIMATED_EXTENSION
        else:
            extension = settings.DEFAULT_STATIC_EXTENSION

        example = template.build_example_url(request, extension=extension)
        self = template.build_self_url(request)
        images.append((example, self))

    return images


def get_test_images(request: Request) -> list[str]:
    animated = utils.urls.flag(request, "animated")
    if animated:
        images = [
            image
            for image in settings.TEST_IMAGES
            if image[2] in settings.ANIMATED_EXTENSIONS
        ]
    else:
        images = settings.TEST_IMAGES

    return [
        request.app.url_for(
            "Images.detail_text",
            template_id=id,
            text_filepath=utils.text.encode(lines) + "." + extension,
        )
        for id, lines, extension in images
    ]
