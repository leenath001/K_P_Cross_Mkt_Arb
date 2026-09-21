"""trade/pinboard/ui.py — Streamlit glue for the Pinnacle live board component."""
import os
import streamlit.components.v1 as components

_board = components.declare_component(
    'pin_board', path=os.path.join(os.path.dirname(os.path.abspath(__file__)), 'frontend'))


def render_board(board):
    """Draw the board. It polls the PinBoard over its own loopback channel; actions only come back through Streamlit
    (setComponentValue) if that channel is unreachable, and are applied exactly once (nonce de-dupe)."""
    act = _board(state=board.snapshot(), key='pin_board', default=None)
    if act and act.get('nonce') != board.ack:
        board.handle_action(act)
        board.ack = act.get('nonce')
