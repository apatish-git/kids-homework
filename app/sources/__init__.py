from .galim import Galim
from .ofek import Ofek
from .smartschool import Smartschool

SOURCES = {s.key: s for s in (Smartschool(), Ofek(), Galim())}
