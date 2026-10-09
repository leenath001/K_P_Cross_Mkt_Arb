"""trade/pinboard/ui.py — Streamlit glue for the Pinnacle live board component."""
import os
import time
import streamlit.components.v1 as components

_board = components.declare_component(
    'pin_board', path=os.path.join(os.path.dirname(os.path.abspath(__file__)), 'frontend'))


def board_args(owner, snapshot):
    """What Streamlit sends the board on each rerun. Normally just how to reach the owner's loopback channel (the board
    fetches its own data there every second): re-sending every card's order book through Streamlit on each rerun was
    pure duplicate work that kept the session busy and the whole dashboard dimmed. Full snapshots are sent only if the
    channel couldn't start, or the board reported it can't reach it (e.g. the page opened from another device)."""
    info = owner.server.info() if getattr(owner, 'server', None) else {}
    if info.get('port') and not getattr(owner, 'need_full', False):
        return {'lite': True, 'ts': time.time(), 'act': info, 'ack': owner.ack}
    return snapshot()


def render_board(board):
    """Draw the board. It polls the PinBoard over its own loopback channel; actions only come back through Streamlit
    (setComponentValue) if that channel is unreachable, and are applied exactly once (nonce de-dupe)."""
    act = _board(state=board_args(board, board.snapshot), key='pin_board', default=None)
    if act and act.get('nonce') != board.ack:
        if act.get('action') == 'need_full':
            board.need_full = True
        else:
            board.handle_action(act)
        board.ack = act.get('nonce')
