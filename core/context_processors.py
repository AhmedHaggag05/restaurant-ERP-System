import os
import time

def project_context(request):
    """
    Adds project-specific environment variables to the template context globally.
    """
    return {
        "project_description": os.getenv("PROJECT_DESCRIPTION", ""),
        "project_image_url": os.getenv("PROJECT_IMAGE_URL", ""),
        # Used for cache-busting static assets
        "deployment_timestamp": int(time.time()),
    }
