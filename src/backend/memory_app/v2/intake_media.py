"""Compatibility module for the shared uploaded-media adapter."""
import sys
from .. import uploaded_media
sys.modules[__name__] = uploaded_media
