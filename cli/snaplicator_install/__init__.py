"""Stepwise install driver — the installer's stages as addressable commands.

deploy/install.sh runs the whole install in one breath, asking its questions
interactively. This package exposes the same stages, in the same order, as
individual commands — so an agent can drive an install, stop at each human
decision, carry the answer in a flag, and resume exactly where it left off.
"""
