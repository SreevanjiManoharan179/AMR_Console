"""Offline AMR Language. No ROS, motion, installations or network at import time."""
from .office import understand
from .office_speech import say
from .office_replies import hear_reply

__all__ = ['understand','say','hear_reply']
__version__ = '0.8.1'
