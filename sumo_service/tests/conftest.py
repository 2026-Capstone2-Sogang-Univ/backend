import sys
import types
from unittest.mock import MagicMock

# Setup traci stub before any test imports app.simulation
if "traci" not in sys.modules or not hasattr(sys.modules["traci"], "_is_stub"):
    traci_mod = types.ModuleType("traci")
    traci_mod._is_stub = True
    for sub in ("simulation", "vehicle", "lane", "edge", "route", "vehicletype"):
        setattr(traci_mod, sub, MagicMock())
    traci_mod.exceptions = types.ModuleType("traci.exceptions")
    traci_mod.exceptions.TraCIException = Exception
    traci_mod.exceptions.FatalTraCIError = Exception
    traci_mod.constants = types.ModuleType("traci.constants")
    
    # Real numeric constants from traci
    constants_dict = {
        "VAR_POSITION": 66,
        "VAR_ANGLE": 67,
        "VAR_SPEED": 64,
        "VAR_DISTANCE": 132,
        "VAR_ROAD_ID": 80,
        "VAR_ROUTE_INDEX": 105,
        "ROUTING_MODE_DEFAULT": 0,
        "ROUTING_MODE_AGGREGATED": 1,
    }
    for attr, val in constants_dict.items():
        setattr(traci_mod.constants, attr, val)
        
    sys.modules["traci"] = traci_mod
    sys.modules["traci.exceptions"] = traci_mod.exceptions
    sys.modules["traci.constants"] = traci_mod.constants
