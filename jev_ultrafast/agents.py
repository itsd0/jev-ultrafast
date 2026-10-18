"""Agent selection. Classic (TypeSafe+Mercury) is the default; JEV_AGENT=new selects Beam."""

import os


def agent_name():
    return "new" if os.environ.get("JEV_AGENT", "classic").strip().lower() == "new" else "classic"


def agent_class():
    if agent_name() == "classic":
        from .agent import Agent

        return Agent
    from .beam_agent import BeamAgent

    return BeamAgent


def default_agent_factory(*args, **kwargs):
    return agent_class()(*args, **kwargs)
