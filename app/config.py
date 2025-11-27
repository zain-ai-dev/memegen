from textwrap import dedent

import bugsnag
from aiohttp.client_exceptions import ClientPayloadError
from PIL import UnidentifiedImageError
from sanic import Request, Sanic
from sanic.exceptions import MethodNotSupported, NotFound
from sanic.handlers import ErrorHandler
from sanic.log import logger

from . import settings, utils, views

IGNORED_EXCEPTIONS = (
    ClientPayloadError,
    MethodNotSupported,
    NotFound,
    UnidentifiedImageError,
)


class BugsnagErrorHandler(ErrorHandler):
    def default(self, request: Request, exception):
        if self._should_notify(exception):
            bugsnag.notify(
                exception,
                metadata={
                    "request": {
                        "method": request.method,
                        "url": request.url,
                        "json": request.json,
                        "form": request.form,
                        "headers": request.headers,
                    }
                },
            )
        return super().default(request, exception)

    @staticmethod
    def _should_notify(exception) -> bool:
        if not settings.BUGSNAG_API_KEY:
            return False
        if isinstance(exception, IGNORED_EXCEPTIONS):
            return False
        return True


def init(app: Sanic):
    app.config.SERVER_NAME = settings.BASE_URL
    app.config.CORS_ORIGINS = "*"
    app.config.CORS_SEND_WILDCARD = True
    app.config.OAS_UI_DEFAULT = "swagger"
    app.config.SWAGGER_UI_CONFIGURATION = {
        "apisSorter": "alpha",
        "operationsSorter": "method",
        "docExpansion": "list",
    }

    app.blueprint(views.examples.blueprint)
    app.blueprint(views.clients.blueprint)
    app.blueprint(views.fonts.blueprint)
    app.blueprint(views.images.blueprint)
    app.blueprint(views.templates.blueprint)
    app.blueprint(views.shortcuts.blueprint)
    
    # Initialize custom meme templates on startup (non-blocking)
    @app.after_server_start
    async def init_custom_templates(app, loop):
        try:
            from ..ai.custom_templates import ensure_custom_templates_initialized
            import asyncio
            # Run in thread pool to not block
            await asyncio.to_thread(ensure_custom_templates_initialized)
            logger.info("Custom meme templates initialized successfully")
        except Exception as e:
            logger.warning(f"Could not initialize custom templates: {e}")

    app.config.MOTD = False
    app.ext._display = lambda: None  # type: ignore

    app.ext.openapi.describe(
        "Meme Generator API",
        version=utils.meta.version(),
        description=dedent(
            """
        ## Quickstart

        Fetch the list of templates:

        ```
        $ http GET https://meme.bigosoft.us/templates

        [
            {
                "id": "aag",
                "name": "Ancient Aliens Guy",
                "lines": 2,
                "overlays": 0,
                "styles": [],
                "blank": "https://meme.bigosoft.us/images/aag.png",
                "example": {
                    "text": [
                        "",
                        "aliens"
                    ],
                    "url": "https://meme.bigosoft.us/images/aag/_/aliens.png"
                },
                "source": "http://knowyourmeme.com/memes/ancient-aliens",
            },
            ...
        ]
        ```

        Add text to create a meme:

        ```
        $ http POST https://meme.bigosoft.us/images template_id=aag "text[]=foo" "text[]=bar"

        {
            "url": "https://meme.bigosoft.us/images/aag/foo/bar.png"
        }
        ```

        View the image: <https://meme.bigosoft.us/images/aag/foo/bar.png>

        ## Links
        """.replace(
                "https://meme.bigosoft.us", settings.BASE_URL
            )
        ),
    )
    app.ext.openapi.contact(name="support", email="support@maketested.com")
    app.ext.openapi.license(
        name="View the license",
        url="https://github.com/jacebrowning/memegen/blob/main/LICENSE.txt",
    )

    app.error_handler = BugsnagErrorHandler()
    bugsnag.configure(
        api_key=settings.BUGSNAG_API_KEY,
        project_root="/app",
        release_stage=settings.RELEASE_STAGE,
    )
