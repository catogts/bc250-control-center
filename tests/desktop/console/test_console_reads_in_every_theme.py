"""The console is dark in every theme, so what is drawn on it has to be light.

Two things disappeared in the light themes. The terminal's Copy/Paste menu got
the console's ground from the rule that paints the grid and its text from the
universal rule that paints the page: dark on dark, so a right click seemed to
do nothing. And the selected tab of several, which carries the console's ground
up into the header, kept the page's dark title on it.
"""

from __future__ import annotations

import pytest
from PyQt6.QtWidgets import QVBoxLayout, QWidget

from frontends.desktop import theme
from frontends.desktop.console.console_panel import ConsolePanel

THEME_STYLES = [(name, style) for name in theme.THEMES for style in theme.STYLES]


@pytest.fixture(autouse=True)
def _restore_theme():
    yield
    theme.configure_theme("light", "blue", "comfortable", 100, style="standard")


@pytest.fixture
def styled_panel(qtbot):
    """A panel in a window that carries the application stylesheet."""
    panels: list[ConsolePanel] = []
    # qtbot only holds a weak reference; without this the window, and the
    # panel in it, were collected as soon as ``build`` returned.
    hosts: list[QWidget] = []

    def build(name: str, style: str) -> ConsolePanel:
        theme.configure_theme(name, "graphite", "comfortable", 100, style=style)
        host = QWidget()
        host.setStyleSheet(theme.application_stylesheet())
        host.resize(1000, 800)
        layout = QVBoxLayout(host)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(QWidget(host), 1)
        panel = ConsolePanel(host)
        layout.addWidget(panel)
        panel.set_auto_hide(False)
        qtbot.addWidget(host)
        host.show()
        hosts.append(host)
        panels.append(panel)
        return panel

    yield build
    for panel in panels:
        panel.shutdown()


def _pair(widget) -> tuple[str, str]:
    widget.ensurePolished()
    palette = widget.palette()
    return (
        palette.color(widget.foregroundRole()).name().upper(),
        palette.color(widget.backgroundRole()).name().upper(),
    )


@pytest.mark.parametrize("name, style", THEME_STYLES)
def test_the_terminal_menu_carries_the_consoles_own_pair(styled_panel, name, style):
    panel = styled_panel(name, style)
    menu = panel.view.context_menu()

    assert _pair(menu) == (
        theme.COLORS["console_text"].upper(),
        theme.COLORS["console_bg"].upper(),
    )
    assert [action.text() for action in menu.actions() if not action.isSeparator()]
    menu.deleteLater()


@pytest.mark.parametrize("name, style", THEME_STYLES)
def test_the_selected_tab_of_several_reads_on_the_consoles_ground(qtbot, styled_panel, name, style):
    panel = styled_panel(name, style)
    assert panel.run(["/bin/sleep", "300"], title="primera")
    with qtbot.waitSignal(panel.workflow_finished, timeout=15000):
        assert panel.run(["/bin/sh", "-c", "exit 2"], title="GFX1013 Fedora kernel + Mesa")
    running, failed = panel._tabs
    assert panel.active_tab is failed

    assert _pair(failed.title_label)[0] == theme.COLORS["console_text"].upper()
    assert _pair(failed.state_label)[0] == theme.COLORS["console_red"].upper()
    # The tab behind it stays on the header, in the page's colours.
    assert _pair(running.title_label)[0] == theme.COLORS["muted"].upper()

    running.activated.emit()
    assert _pair(running.title_label)[0] == theme.COLORS["console_text"].upper()
    assert _pair(running.state_label)[0] == theme.COLORS["console_blue"].upper()
    assert _pair(failed.title_label)[0] == theme.COLORS["muted"].upper()
    assert _pair(failed.state_label)[0] == theme.COLORS["red"].upper()
    panel.shutdown()


def test_a_lone_tab_keeps_the_headers_colours(qtbot, styled_panel):
    """Alone it sits on the header, not on the console."""
    panel = styled_panel("light", "formal")
    with qtbot.waitSignal(panel.workflow_finished, timeout=15000):
        assert panel.run(["/bin/sh", "-c", "exit 2"], title="GFX1013 Fedora kernel + Mesa")

    assert panel.tab_count() == 1
    assert _pair(panel.active_tab.title_label)[0] == theme.COLORS["text"].upper()
    assert _pair(panel.active_tab.state_label)[0] == theme.COLORS["red"].upper()
