"""Home navigation and repainting must preserve existing command/protocol contracts."""
import io
import os
import subprocess

import pytest
from typer.main import get_command
from typer.testing import CliRunner

from adapt_adb.cli import app
from adapt_adb import terminal_ui as ui


class Terminal(io.StringIO):
    def isatty(self): return True


@pytest.fixture
def terminal(monkeypatch):
    monkeypatch.delenv("ADAPT_NO_BANNER", raising=False)
    monkeypatch.delenv("ADAPT_BANNER", raising=False)
    monkeypatch.delenv("TERM", raising=False)
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setattr(ui.shutil,"get_terminal_size",lambda **kwargs:os.terminal_size((100,32)))
    restored=[]
    monkeypatch.setattr(ui,"_enable_vt",lambda:(True,lambda:restored.append(True)))
    monkeypatch.setattr(ui.subprocess,"run",lambda *a,**k:pytest.fail("Opening a menu must not run any command"))
    return Terminal(), Terminal(), restored


def run_keys(keys, terminal, **kwargs):
    out, err, restored=terminal
    ui.run_console(get_command(app),stdin=Terminal(keys),stdout=out,stderr=err,**kwargs)
    return out.getvalue(),err.getvalue(),restored


def test_home_rapid_back_and_home_redraw_without_running_commands(terminal):
    out,err,restored=run_keys("7\n1\nb\nh\nq\n",terminal)
    assert out.count("ADAPT / MAIN MENU")==2
    assert "ADAPT / RAPID" in out
    assert out.count("\x1b[H\x1b[2J")>=5
    assert out.startswith("\x1b[?1049h") and out.endswith("\x1b[?1049l")
    assert "[H] Home" in out and "[B] Back" in out
    assert "ANDROID DEVICE AGENT PROVISIONING TOOLKIT" in err and "\x1b[38;5;46m" in err
    assert restored==[True]


def test_paging_reaches_large_body_without_growing_screen_content(terminal):
    out,err,_=terminal
    with ui.Screen(Terminal(),out,err) as screen:
        _,pages=screen.draw("LONG VIEW",[f"record-{i:03}" for i in range(100)])
        page,_=screen.draw("LONG VIEW",[f"record-{i:03}" for i in range(100)],999)
    assert pages>1 and page==pages-1
    frames=out.getvalue().split("\x1b[H\x1b[2J")[1:]
    assert "record-099" in frames[-1] and "record-000" not in frames[-1]
    assert all(len(frame.splitlines())<=32 for frame in frames)


def test_home_keeps_all_areas_visible_in_standard_80_by_24_terminal(terminal,monkeypatch):
    monkeypatch.setattr(ui.shutil,"get_terminal_size",lambda **kwargs:os.terminal_size((80,24)))
    out,err,_=run_keys("q\n",terminal)
    assert "[9] All ADAPT commands" in out and "Page 1/1" in out
    assert "ANDROID DEVICE AGENT PROVISIONING TOOLKIT" in err


def test_no_redraw_basic_terminal_and_banner_suppression(terminal,monkeypatch):
    out,err,_=run_keys("h\nq\n",terminal,no_redraw=True,no_banner=True)
    assert "\x1b" not in out+err and "ADAPT / MAIN MENU" in out
    terminal[0].seek(0);terminal[0].truncate();terminal[1].seek(0);terminal[1].truncate()
    monkeypatch.setenv("TERM","dumb")
    out,err,_=run_keys("q\n",terminal)
    assert "\x1b" not in out+err


def test_color_opt_out_keeps_original_banner_shape_and_home(terminal,monkeypatch):
    monkeypatch.setenv("NO_COLOR","1")
    out,err,_=run_keys("q\n",terminal)
    assert "ADAPT / MAIN MENU" in out and "\x1b" not in err
    assert "ANDROID DEVICE AGENT PROVISIONING TOOLKIT" in err


def test_screen_sanitizes_controls_and_keeps_newlines(terminal):
    out,err,_=terminal
    with ui.Screen(Terminal(),out,err,no_banner=True) as screen:
        screen.draw("DETAIL",["first\nsecond\x1b[3J"])
    assert "first\nsecond\\x1b[3J" in out.getvalue() and "\x1b[3J" not in out.getvalue()


def test_running_explicit_command_keeps_argv_prompts_and_root_options(terminal,monkeypatch):
    # Logs area -> tips -> run -> arguments, then H and Q. No shell involved.
    calls=[]
    monkeypatch.setattr(ui.subprocess,"run",lambda argv,**kwargs:calls.append((argv,kwargs)) or subprocess.CompletedProcess(argv,0))
    root=get_command(app)
    records=ui.command_inventory(root)
    area=[r for r in records if r['command'].count(' ')==1 and r['command'].split()[1] in ui.AREAS[7][1]]
    index=next(i for i,r in enumerate(area,1) if r['command']=='adapt tips')
    out,err,_=run_keys(f"8\n{index}\nr\nstatus\nh\nq\n",terminal,no_tips=True,no_progress=True)
    argv,kwargs=calls[0]
    assert argv[-2:]==["tips","status"] and "--no-tips" in argv and "--no-progress" in argv
    assert kwargs=={"check":False} and "shell" not in kwargs
    assert out.count("ADAPT / MAIN MENU")==2


def test_platform_argument_parser_preserves_quoted_paths_and_never_executes_shell():
    assert ui.split_arguments('--snapshot "C:\\some folder\\awake.json"') == ["--snapshot", "C:\\some folder\\awake.json"]
    assert ui.split_arguments('hello ; echo no') == ["hello",";","echo","no"]
    with pytest.raises(ValueError): ui.split_arguments("bad\x1b[2J")


def test_redirected_console_and_bare_invocation_remain_machine_safe():
    runner=CliRunner()
    result=runner.invoke(app,["console"])
    assert result.exit_code==2 and "needs a terminal" in result.stderr
    assert "\x1b" not in result.stdout+result.stderr
    result=runner.invoke(app,[])
    assert result.exit_code==2 and "Usage:" in result.stdout
    result=runner.invoke(app,["help-me","--all","--json"])
    import json
    paths={r['command'] for r in json.loads(result.stdout)['commands']}
    assert {"adapt doctor","adapt setup","adapt mcp","adapt adb","adapt dex mirror","adapt console"} <= paths
    assert result.exit_code==0 and "\x1b" not in result.stdout and not result.stderr


def test_interactive_bare_adapt_opens_same_home_and_passes_options(monkeypatch):
    calls=[]
    monkeypatch.setattr('adapt_adb.cli._is_interactive_session',lambda:True)
    monkeypatch.setattr(ui,"run_console",lambda root,**kwargs:calls.append(kwargs))
    result=CliRunner().invoke(app,["--no-banner","--no-tips"])
    assert result.exit_code==0 and calls[0]['no_banner'] and calls[0]['no_tips']
