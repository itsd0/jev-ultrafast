"""Agent selection. JEV_AGENT=classic keeps TypeSafe+Mercury; default is Beam."""

import os


def agent_name():
    return "classic" if os.environ.get("JEV_AGENT", "new").strip().lower() == "classic" else "new"


def agent_class():
    if agent_name() == "classic":
        from .agent import Agent

        return Agent
    from .beam_agent import BeamAgent

    return BeamAgent


def default_agent_factory(*args, **kwargs):
    return agent_class()(*args, **kwargs)
