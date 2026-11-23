"""Texts that describe.py writes when a file or folder has no docstring of its own."""
import pytest

from codevision import describe
from codevision.graph import build
from codevision.model import FileFacts, Symbol


def facts(path, *symbols, language="python", doc=""):
    return FileFacts(path=path, language=language, module_doc=doc, symbols=list(symbols))


def fn(name, doc="", exported=False):
    return Symbol(name, "function", f"{name}()", doc=doc, exported=exported)


def cls(name, doc="", exported=False):
    return Symbol(name, "class", f"class {name}", doc=doc, exported=exported)


def s_of(rel, ff, area="sv", **kw):
    return describe.describe_file(rel, ff, area=area, used_by=1, **kw)[0]


# ---------------------------------------------------------------- D1: what the folder or main class shows

def test_a_file_in_a_commands_folder_is_that_command():
    install = facts("pip/_internal/commands/install.py",
                    cls("InstallCommand", "Install packages from:", exported=True), fn("get_lib_location_guesses"))
    s, d = describe.describe_file("pip/_internal/commands/install.py", install, area="sv", uses=29, used_by=2)
    assert (s, d) == ("install 명령", "install 명령이 할 일을 정해 둔 코드예요. 다른 파일 29개를 불러 쓰고, 2개 파일이 이 파일을 써요.")
    assert s_of("src/commands/build.js", facts("src/commands/build.js", fn("build", exported=True),
                                              language="javascript")) == "build 명령"
    # what the commands share is not one command; the folder above still says something
    assert s_of("tool/cli/commands/base.py", facts("tool/cli/commands/base.py", cls("Command"))) == "명령어 실행 부분"
    assert s_of("tool/commands/_helpers.py", facts("tool/commands/_helpers.py", fn("run"))) != "_helpers 명령"


@pytest.mark.parametrize("rel, area, s", [
    ("server/routes/users.py", "sv", "주소별 요청 처리"),
    ("app/services/mailer.py", "sv", "핵심 기능 처리"),
    ("web/src/components/format.js", "ui", "화면 부품 코드"),
    ("pip/_internal/cli/spinners.py", "sv", "명령어 실행 부분"),
    ("pip/_internal/network/session.py", "sv", "네트워크 통신"),
    ("pip/_internal/vcs/git.py", "sv", "버전 관리 도구 연결"),
    ("app/plugins/loader.py", "sv", "추가 기능(플러그인)"),
    ("app/auth.py", "sv", "로그인·권한 처리"),  # the file's own name
    ("pip/_internal/pyproject.py", "sv", "내부 코드"),  # a weak role: only for the folder right above
    ("web/frontend/boot.js", "ui", "화면 쪽 코드"),
])
def test_folder_roles_come_before_the_generic_fallback(rel, area, s):
    language = "javascript" if rel.endswith(".js") else "python"
    assert s_of(rel, facts(rel, fn("run"), fn("stop"), language=language), area) == s


def test_screen_folder_names_do_not_describe_python_code():
    ff = facts("pygments/styles/monokai.py", cls("MonokaiStyle"))
    assert s_of("pygments/styles/monokai.py", ff) == "Python 코드"  # not "화면 꾸미기 코드"
    # "server/" says "서버 쪽 코드" only for code on the server side
    assert s_of("app/server/boot.js", facts("app/server/boot.js", fn("a"), fn("b"), language="javascript"),
                area="ui") == "JavaScript 코드"


def test_the_main_class_docstring_when_the_module_has_none():
    ff = facts("rich/align.py", fn("_helper", "Pad a line."),
               cls("Align", "Align a renderable by adding spaces if necessary.", exported=True),
               cls("VerticalCenter", "Vertically aligns a renderable.", exported=True))
    s, d = describe.describe_file("rich/align.py", ff, area="sv", used_by=4)
    assert s == describe.short("Align a renderable by adding spaces if necessary.")
    assert d == ("Align a renderable by adding spaces if necessary. 이 파일의 대표 클래스인 Align에 적힌 설명이에요. "
                 "4개 파일이 이 파일을 써요.")
    only = facts("pip/_internal/operations/build/wheel.py", fn("build_wheel_pep517", "Build one wheel."))
    assert describe.describe_file("pip/_internal/operations/build/wheel.py", only)[1].startswith(
        "Build one wheel. 이 파일의 대표 함수인 build_wheel_pep517에 적힌 설명이에요.")
    # the only public class, with the error it raises beside it
    manager = facts("pluggy/_manager.py", fn("_warn"), cls("PluginValidationError", "Plugin failed validation.", True),
                    cls("PluginManager", "Core class which manages plugins.", True))
    assert s_of("pluggy/_manager.py", manager) == "Core class which manages plugins"
    linked = facts("pd/windows.py", cls("Windows", "MSDN on where to store app data files <https://learn.microsoft.com/x>."))
    assert describe.describe_file("pd/windows.py", linked)[1].startswith("MSDN on where to store app data files. 이 파일의")


def test_no_main_class_is_made_up():
    two = facts("pkg/shapes.py", cls("Circle", "A circle."), cls("Square", "A square."))
    assert s_of("pkg/shapes.py", two) == "Python 코드"
    # warn() among warning classes is not the file's only public name
    warnings = facts("pkg/warning_types.py", cls("FooWarning", "A foo warning."), cls("BarWarning"),
                     fn("warn_explicit", "Issue the warning."))
    assert s_of("pkg/warning_types.py", warnings) == "Python 코드"
    undocumented = facts("pkg/git.py", cls("Git"), fn("looks_like_hash"))
    assert describe.describe_file("pkg/git.py", undocumented, area="sv", used_by=1) == (
        "Python 코드", "함수나 클래스 2개가 들어 있는 Python 코드예요. 1개 파일이 이 파일을 써요.")


def test_error_files_and_copied_in_code():
    assert s_of("pkg/errors.py", facts("pkg/errors.py", cls("ConfigError"), fn("explain"))) == "오류 종류 정의"
    assert s_of("pkg/problems.py", facts("pkg/problems.py", cls("AError"), cls("BException"))) == "오류 종류 정의"
    assert s_of("pkg/notices.py", facts("pkg/notices.py", cls("AWarning"), cls("BWarning"))) != "오류 종류 정의"
    cells = facts("pip/_vendor/rich/cells.py", fn("cell_len"), fn("set_cell_size"))
    s, d = describe.describe_file("pip/_vendor/rich/cells.py", cells, area="sv", uses=1, used_by=31)
    assert s == "rich에서 가져온 코드"
    assert d == "다른 곳(rich)에서 가져와 그대로 넣어 둔 코드예요. 다른 파일 1개를 불러 쓰고, 31개 파일이 이 파일을 써요."


def test_a_module_docstring_still_comes_first():
    ff = facts("pkg/commands/install.py", cls("InstallCommand", "Install things."), doc="패키지를 설치하는 명령이에요.")
    assert s_of("pkg/commands/install.py", ff) == "패키지를 설치하는 명령이에요"


# ---------------------------------------------------------------- D2: summaries that only lead into a list

@pytest.mark.parametrize("text, shown", [
    ("Spinners are from:", ""),
    ("Install packages from:", ""),
    ("From RFC7231:", ""),
    ("Usage:", ""),
    ("이 모듈은 다음과 같은 일을 해요:", ""),
    ("For PsySH console output, such as:", "For PsySH console output"),
    ("For Elixir interactive console (iex) output like:", "For Elixir interactive console (iex) output"),
    ("Format text with a color and/or some attributes::", "Format text with a color and/or some attributes"),
    ("A pluggy.PluginManager with additional pytest-specific functionality:",
     "A pluggy.PluginManager with additional pytest-specific functionality"),
    ("할 일 목록 API:", "할 일 목록 API"),
    ("On Unix/Linux, we follow the XDG Basedir Spec <https://specifications.freedesktop.org/basedir/>.",
     "On Unix/Linux, we follow the XDG Basedir Spec."),
    ("Plain sentence.", "Plain sentence."),
    ("The following options are supported:", ""),
    ("이 모듈은 다음 기능을 제공해요:", ""),
    ("Helpers for following symlinks:", "Helpers for following symlinks"),
])
def test_lead_in_summaries(text, shown):
    assert describe._doc_text(text) == shown
    assert describe.is_lead_in(text) == (not shown)


@pytest.mark.parametrize("text, shown", [
    # the analyzer stops where the next docstring line starts with ":class:`...`" or a quoted example
    ("Base class for all connection pools, such as", "Base class for all connection pools"),
    ("Convenience mixin for classes who implement a urlopen method, such as urllib3.HTTPConnectionPool and",
     "Convenience mixin for classes who implement a urlopen method, such as urllib3.HTTPConnectionPool"),
    ("Renders completed count/total, e.g.", "Renders completed count/total"),
    ("A chain of exceptions, separated by descriptions (e.g.", "A chain of exceptions, separated by descriptions"),
    ("Obtain a new instance of the", "Obtain a new instance"),
    ("Usage and", ""),
    # whole sentences stay as they are
    ("Base class that all auth implementations derive from", "Base class that all auth implementations derive from"),
    ("Compute the logical or", "Compute the logical or"),
    ("Return the square of a", "Return the square of a"),
    ("Defined in Appendix A.", "Defined in Appendix A."),
    # a Sphinx reference keeps the name Sphinx shows; a home folder path is not one
    ("A container for style information, used by ~rich.console.Console.",
     "A container for style information, used by Console."),
    ("Read settings from ~/.config/tool.toml.", "Read settings from ~/.config/tool.toml."),
])
def test_summaries_cut_before_their_examples(text, shown):
    assert describe._doc_text(text) == shown


def test_only_summaries_that_can_be_cut_are_searched(monkeypatch):
    # the cut-ending search tries every position of the text; it took most of the time describe.py
    # spends on a project (6 of 19 ms for pip), so a summary ending with none of its words skips it
    real, searched = describe._CUT_TAIL, []

    class Counting:
        def search(self, text):
            searched.append(text)
            return real.search(text)

    monkeypatch.setattr(describe, "_CUT_TAIL", Counting())
    for text in ("Return the parsed config.", "Base class that all auth implementations derive from", "", None,
                 "Read settings from ~/.config/tool.toml.", "Lexer for Elpi <http://example.org/elpi>."):
        describe._doc_text(text)
    assert searched == []
    assert describe._doc_text("Renders completed count/total, e.g.") == "Renders completed count/total"
    assert describe._doc_text("Obtain a new instance of the") == "Obtain a new instance"
    assert searched  # and these were


def test_a_cut_main_class_docstring_reads_as_a_whole_phrase():
    pool = facts("urllib3/connectionpool.py", cls("ConnectionPool", "Base class for all connection pools, such as"))
    s, d = describe.describe_file("urllib3/connectionpool.py", pool, area="sv", used_by=10)
    assert s == "Base class for all connection pools"
    assert d == ("Base class for all connection pools. 이 파일의 대표 클래스인 ConnectionPool에 적힌 설명이에요. "
                 "10개 파일이 이 파일을 써요.")
    assert describe.functions(pool) == [["class ConnectionPool", "Base class for all connection pools"]]


def test_a_lead_in_docstring_falls_through_to_the_next_rule():
    spin = facts("pip/_vendor/rich/_spinners.py", doc="Spinners are from:")
    s, d = describe.describe_file("pip/_vendor/rich/_spinners.py", spin, area="sv", used_by=1)
    assert s == "rich에서 가져온 코드" and "Spinners" not in d
    assert describe.describe_file("spinners.py", facts("spinners.py", doc="Spinners are from:"))[0] == "Python 코드"
    assert describe.describe_folder("engine", area="sv", package=True, doc="Helpers for:")[0] == "서버 코드 모음"
    assert describe.describe_root("x", doc="Spinners are from:", languages=["Python"])[0] == "Python 프로젝트"


def test_function_descriptions_drop_lead_ins():
    ff = facts("x.py", cls("InstallCommand", "Install packages from:"),
               fn("ansiformat", "Format text with some attributes::"),
               Symbol("items", "function", "items()", doc="From RFC7231:", hint="GET · /items"))
    assert [f[1] for f in describe.functions(ff)] == ["", "Format text with some attributes", "GET · /items"]


def test_readme_lead_in_and_the_block_under_it_are_passed_over():
    readme = "# Tool\n\nInstall it with:\n\n    pip install tool\n\nTool draws projects.\n"
    assert describe.readme_line(readme) == "Tool draws projects."
    fenced = "Install it with:\n\n```\npip install tool\n```\n\nTool draws projects.\n"
    assert describe.readme_line(fenced) == "Tool draws projects."
    assert describe.readme_line("Features:\n- fast\n- small\n\nTool draws projects.\n") == "Tool draws projects."
    assert describe.readme_line("Only a lead-in, see:\n") == ""
    assert describe.describe_root("tool", readme=readme)[0] == "Tool draws projects"


# ---------------------------------------------------------------- D3: pictures and design material

@pytest.mark.parametrize("rel, used_by, d", [
    ("design/screenshots/home.png", 0, "프로그램 화면을 찍어 둔 그림이에요."),
    ("design/reference.png", 0, "디자인 자료로 넣어 둔 그림이에요."),
    ("mockups/login.jpg", 0, "디자인 자료로 넣어 둔 그림이에요."),
    ("notes/photo.jpg", 0, "그림 파일이에요."),
    ("photo.png", 0, "그림 파일이에요."),
    ("web/public/logo.png", 0, "화면에 쓰이는 그림이나 아이콘이에요."),
    ("src/assets/icons/close.png", 0, "화면에 쓰이는 그림이나 아이콘이에요."),
    ("src/logo.png", 1, "화면에 쓰이는 그림이나 아이콘이에요. 1개 파일이 이 파일을 써요."),
    # an images/ folder of the docs or the tests is not the screen's
    ("docs/images/architecture.png", 0, "설명 문서와 함께 둔 그림이에요."),
    ("tests/fixtures/img/cat.png", 0, "테스트할 때 쓰는 그림이에요."),
])
def test_image_texts_follow_the_evidence(rel, used_by, d):
    assert describe.describe_file(rel, binary=True, used_by=used_by) == ("이미지 파일", d)


def test_svg_images_are_text_files_with_the_same_texts():
    assert describe.describe_file("notes/diagram.svg") == ("이미지 파일", "그림 파일이에요.")
    assert describe.describe_file("docs/diagram.svg") == ("이미지 파일", "설명 문서와 함께 둔 그림이에요.")
    assert describe.describe_file("src/assets/logo.svg")[1] == "화면에 쓰이는 그림이나 아이콘이에요."


def test_picture_and_design_folders():
    assert describe.describe_folder("design/screenshots", files=5) == (
        "화면 캡처 모음", "프로그램 화면을 찍어 둔 그림이 모여 있어요. 파일 5개가 들어 있어요.")
    assert describe.describe_folder("design", area="ui", files=2, folders=1)[0] == "디자인 자료 모음"
    assert describe.describe_folder("wireframes", files=3)[0] == "디자인 자료 모음"
    assert describe.describe_folder("app/design", area="sv", package=True)[0] == "서버 코드 모음"  # Python code
    assert describe.describe_folder("photos", files=3, images=3) == ("그림 모음", "그림 파일이 모여 있어요. 파일 3개가 들어 있어요.")
    assert describe.describe_folder("photos", files=3, images=2)[0] == "문서·설정 모음"
    assert describe.describe_folder("docs/images", files=2) == (
        "이미지 모음", "설명 문서와 함께 둔 그림이 모여 있어요. 파일 2개가 들어 있어요.")
    assert describe.describe_folder("src/assets/images", files=2)[1].startswith("화면에 쓰는 그림과 아이콘이")


def test_a_design_folder_whose_code_is_used_elsewhere_is_code():
    # src/design/ of a React app: Button.tsx and tokens.ts that App.tsx imports are program code
    assert describe.describe_folder("src/design", area="ui", files=2, area_label="화면 코드", used_outside=2) == (
        "화면 코드 모음", "파일 2개가 들어 있어요.")
    assert describe.describe_folder("mockups", area="ui", files=1, area_label="화면 코드", used_outside=1)[0] == "화면 코드 모음"
    # nothing outside uses it (the default for callers that do not know): design material, as before
    assert describe.describe_folder("src/design", area="ui", files=2, area_label="화면 코드")[0] == "디자인 자료 모음"
    # other folder names do not change with it
    assert describe.describe_folder("src/components", area="ui", files=2, used_outside=2)[0] == "화면 부품 모음"


def test_pictures_in_a_built_graph(tmp_path):
    png = b"\x89PNG\r\n\x1a\n" + bytes(32)
    shots = tmp_path / "design" / "screenshots"
    shots.mkdir(parents=True)
    for name in ("light.png", "dark.png"):
        (shots / name).write_bytes(png)
    (tmp_path / "design" / "reference.png").write_bytes(png)
    (tmp_path / "design" / "mockup.html").write_text("<!doctype html><title>mockup</title>\n", encoding="utf-8")
    web = tmp_path / "web"
    web.mkdir()
    (web / "logo.png").write_bytes(png)
    (web / "index.html").write_text('<!doctype html><link rel="icon" href="logo.png">\n', encoding="utf-8")
    (tmp_path / "notes").mkdir()
    (tmp_path / "notes" / "photo.png").write_bytes(png)

    nodes = build(str(tmp_path))["nodes"]
    assert nodes["design"]["s"] == "디자인 자료 모음"
    assert nodes["design/screenshots"]["s"] == "화면 캡처 모음"
    assert nodes["design/screenshots/light.png"]["d"] == "프로그램 화면을 찍어 둔 그림이에요."
    assert nodes["design/reference.png"]["d"] == "디자인 자료로 넣어 둔 그림이에요."
    assert nodes["notes/photo.png"]["d"] == "그림 파일이에요."
    assert nodes["web/logo.png"]["b"] == ["web/index.html"]
    assert nodes["web/logo.png"]["d"] == "화면에 쓰이는 그림이나 아이콘이에요. 1개 파일이 이 파일을 써요."


# ---------------------------------------------------------------- D4: thousands separators

def test_counts_use_thousands_separators():
    many = [fn(f"f{i}") for i in range(1234)]
    assert "함수나 클래스 1,234개가 들어 있는 Python 코드예요." in describe.describe_file("big.py", facts("big.py", *many))[1]
    assert "함수나 클래스 1,234개가 들어 있어요." in describe.describe_file("pkg/__init__.py", facts("pkg/__init__.py", *many))[1]
    assert describe.describe_file("logo.png", binary=True, uses=0, used_by=2345)[1].endswith("2,345개 파일이 이 파일을 써요.")
    assert describe.describe_folder("big", files=12345, folders=1000)[1] == "파일 12,345개, 폴더 1,000개가 들어 있어요."
    assert describe.describe_root("x", languages=["Python"], files=5000, folders=1200)[1] == (
        "Python 코드로 된 프로젝트예요. 파일 5,000개, 폴더 1,200개가 들어 있어요.")


# ---------------------------------------------------------------- joining wrapped lines

def test_join_wrapped_looks_at_each_line_once(monkeypatch):
    # a sentence wrapped over 2,000 lines: the whole text joined so far was searched again for a
    # sentence end before each line (74 million characters here; 10,000 longer lines took 90 s)
    real = describe._SENTENCE_BREAK
    searched = []

    class Counting:
        def search(self, text):
            searched.append(len(text))
            return real.search(text)

        def split(self, text, maxsplit=0):
            return real.split(text, maxsplit=maxsplit)

    monkeypatch.setattr(describe, "_SENTENCE_BREAK", Counting())
    lines = [f"part{i} of one sentence that goes on" for i in range(2000)]
    assert describe.join_wrapped(iter(lines)).startswith("part0 of one sentence that goes on part1 of")
    assert sum(searched) < 2 * sum(len(line) + 1 for line in lines)


@pytest.mark.parametrize("lines, joined", [
    (["The distro package provides", "information about the system."], "The distro package provides information about the system."),
    (["Runs the job.", "then stops"], "Runs the job."),  # ends a sentence: the next line is not joined
    (["a b", "c. D", "e"], "a b c."),  # a sentence that ends inside a joined line
    (["a b", "c. d", "e"], "a b c. d e"),  # "c. d" goes on ("e.g. this")
    (["Fast", "- list item"], "Fast"),
    (["  ", "첫 줄이", "이어져요."], "첫 줄이 이어져요."),
    ([], ""),
])
def test_join_wrapped(lines, joined):
    assert describe.join_wrapped(lines) == joined


# ---------------------------------------------------------------- Vue and Svelte files, framework files (2026-11-20)

@pytest.mark.parametrize("rel, s, d", [
    ("src/components/TodoItem.vue", "Vue 화면 조각", "화면의 한 부분(TodoItem)을 그리는 Vue 코드예요."),
    ("src/lib/Counter.svelte", "Svelte 화면 조각", "화면의 한 부분(Counter)을 그리는 Svelte 코드예요."),
    ("src/components/DatePicker/index.vue", "Vue 화면 조각", "화면의 한 부분(DatePicker)을 그리는 Vue 코드예요."),
    ("src/App.vue", "화면 전체 틀", "화면 조각들을 모아 전체 화면을 짜는 뼈대예요. Vue로 만들었어요."),
    ("pages/todos/[id].vue", "화면 페이지", "주소마다 보이는 화면 한 페이지예요. Vue로 만들었어요."),
    ("src/routes/+page.svelte", "화면 페이지", "주소마다 보이는 화면 한 페이지예요. Svelte로 만들었어요."),
    ("layouts/default.vue", "화면 배치 틀", "여러 페이지가 같이 쓰는 화면 배치를 정해요. Vue로 만들었어요."),
    ("src/routes/admin/+layout@.svelte", "화면 배치 틀",
     "여러 페이지가 같이 쓰는 화면 배치를 정해요. Svelte로 만들었어요."),
    ("error.vue", "오류 화면", "문제가 생겼을 때 대신 보여 주는 화면이에요. Vue로 만들었어요."),
    # Nuxt 4 keeps the app in app/ (2026-11-21: its error.vue was "화면의 한 부분(error)")
    ("app/error.vue", "오류 화면", "문제가 생겼을 때 대신 보여 주는 화면이에요. Vue로 만들었어요."),
    ("components/Error.vue", "Vue 화면 조각", "화면의 한 부분(Error)을 그리는 Vue 코드예요."),
    ("src/routes/+error.svelte", "오류 화면", "문제가 생겼을 때 대신 보여 주는 화면이에요. Svelte로 만들었어요."),
])
def test_vue_and_svelte_files_are_screen_parts_named_after_the_component(rel, s, d):
    ff = facts(rel, fn("load"), language="typescript")
    assert describe.describe_file(rel, ff, area="ui", used_by=1) == (s, d + " 1개 파일이 이 파일을 써요.")
    # not analyzed (no JavaScript analyzer): still the framework's screen part
    assert describe.describe_file("src/A.svelte", None, area="ui")[0] == "Svelte 화면 조각"


def test_a_file_a_framework_loads_says_so():
    ff = facts("components/AppHeader.vue", language="javascript")
    note = "Nuxt가 화면에 쓰인 이름을 보고 알아서 불러와요."
    assert describe.describe_file("components/AppHeader.vue", ff, area="ui", loaded=note) == (
        "Vue 화면 조각", "화면의 한 부분(AppHeader)을 그리는 Vue 코드예요. " + note)  # not "직접 이어져 있지 않아요"
    assert describe.describe_file("components/AppHeader.vue", ff, area="ui", loaded=note, uses=2)[1].endswith(
        note + " 다른 파일 2개를 불러 써요.")
    # a start point says why it starts instead
    assert describe.describe_file("app.vue", facts("app.vue", language="javascript"), area="ui", entry=True,
                                  entry_reason="Nuxt가 처음 띄우는 화면", loaded="Nuxt가 알아서 불러와요.") == (
        "화면 시작점", "실행하면 여기서부터 시작돼요(Nuxt가 처음 띄우는 화면).")


@pytest.mark.parametrize("rel, ff, s", [
    ("src/routes/+page.ts", facts("src/routes/+page.ts", fn("load"), language="typescript"), "페이지 데이터 준비"),
    ("src/hooks.server.ts", facts("src/hooks.server.ts", fn("handle"), language="typescript"), "요청 중간 처리"),
    ("composables/useTodos.ts", facts("composables/useTodos.ts", fn("useTodos"), language="typescript"),
     "화면 동작 도우미(훅)"),
])
def test_framework_code_files_get_their_own_roles(rel, ff, s):
    got_s, got_d = describe.describe_file(rel, ff, area="ui", used_by=1)
    assert got_s == s and "React" not in got_d
    server = describe.describe_file("src/routes/x/+page.server.ts", facts("src/routes/x/+page.server.ts",
                                                                          language="typescript"), area="ui", used_by=1)
    assert server[1].startswith("화면에 보여 줄 데이터를 서버에서 미리 불러오는 코드예요.")
    react = facts("src/hooks/useTodos.js", fn("useTodos"), language="javascript")
    assert "React 훅" in describe.describe_file("src/hooks/useTodos.js", react, area="ui", used_by=1)[1]


def test_stack_names_for_nuxt_and_sveltekit():
    assert [describe.UI_FRAMEWORKS[k] for k in ("vue", "nuxt", "svelte", "sveltekit")] == [
        "Vue", "Nuxt", "Svelte", "SvelteKit"]
    assert list(describe.UI_FRAMEWORKS).index("nuxt") > list(describe.UI_FRAMEWORKS).index("vue")


# ---------------------------------------------------------------- the main names before the error classes (2026-11-21)

def err(name, *bases, doc="", exported=True):
    return Symbol(name, "class", f"class {name}", doc=doc, exported=exported, bases=bases)


def listed(ff):
    return [f[0] for f in describe.functions(ff)]


def smtplib_symbols():
    """smtplib.py of the standard library: ten error classes (three named only by what they are made
    from), then what the module is for. LMTP is not in __all__, but has no leading underscore either."""
    return (
        err("SMTPException", "OSError"), err("SMTPNotSupportedError", "SMTPException"),
        err("SMTPServerDisconnected", "SMTPException"), err("SMTPResponseException", "SMTPException"),
        err("SMTPSenderRefused", "SMTPResponseException"), err("SMTPRecipientsRefused", "SMTPException"),
        err("SMTPDataError", "SMTPResponseException"), err("SMTPConnectError", "SMTPResponseException"),
        err("SMTPHeloError", "SMTPResponseException"), err("SMTPAuthenticationError", "SMTPResponseException"),
        fn("quoteaddr", exported=True), fn("_addr_only"), fn("quotedata", exported=True), fn("_quote_periods"),
        fn("_fix_eols"), cls("SMTP", "This class manages a connection to an SMTP or ESMTP server.", exported=True),
        cls("LMTP"),
    )


def test_the_main_names_come_before_the_error_classes():
    # 2026-11-21: the panel took the public names in source order, so smtplib.py listed eight of its error
    # classes and never class SMTP, and the AI was asked about those eight only
    assert listed(facts("smtplib.py", *smtplib_symbols())) == [
        "quoteaddr()", "quotedata()", "class SMTP", "class LMTP",
        "class SMTPException", "class SMTPNotSupportedError", "class SMTPServerDisconnected",
        "class SMTPResponseException"]
    # by name alone, in Python and JavaScript alike
    loader = facts("loader.py", cls("ConfigError", exported=True), cls("ParseWarning", exported=True),
                   cls("LoadException", exported=True), fn("load", exported=True), cls("Loader", exported=True))
    assert listed(loader) == ["load()", "class Loader", "class ConfigError", "class ParseWarning", "class LoadException"]
    api = facts("src/api.ts", cls("ApiError", exported=True), fn("getTodos", exported=True), language="typescript")
    assert listed(api) == ["getTodos()", "class ApiError"]


def test_error_classes_are_known_by_what_they_are_made_from():
    # one of Python's own, whose names do not say so; one named so in another module (errors.SendError is
    # recorded as "SendError"); one defined earlier in this file. A plain name from another module
    # (Rejected) and other bases say nothing.
    ftp = facts("ftp.py", err("Interrupted", "KeyboardInterrupt"), err("Finished", "StopIteration"),
                err("Refused", "SendError"), err("error_temp", "Refused"), err("Session", "Base"),
                err("Bounced", "Rejected"), cls("FTP", exported=True))
    assert listed(ftp) == ["class Session", "class Bounced", "class FTP", "class Interrupted", "class Finished",
                           "class Refused", "class error_temp"]


def test_error_classes_still_come_before_the_private_helpers():
    ff = facts("tool.py", fn("_warn", "Warn once."), err("_Stop", "Exception", exported=False), fn("_check"),
               cls("ToolError", exported=True), cls("_Helper"), cls("Tool", exported=True))
    assert listed(ff) == ["class Tool", "class ToolError", "_warn()", "class _Stop", "class _Helper", "_check()"]
    # only the room left: eight main names leave none for the errors
    many = facts("big.py", cls("BigError", exported=True), *(fn(f"run{i}", exported=True) for i in range(9)))
    assert "class BigError" not in listed(many) and len(listed(many)) == 8


def test_a_module_of_error_classes_still_lists_them_in_source_order():
    click = facts("click/exceptions.py", fn("_join_param_hints", "Join the hints."),
                  err("ClickException", "Exception"), err("UsageError", "ClickException"),
                  err("BadParameter", "UsageError"), err("Abort", "RuntimeError"), err("Exit", "RuntimeError"))
    assert listed(click) == ["class ClickException", "class UsageError", "class BadParameter", "class Abort",
                             "class Exit", "_join_param_hints()"]


def test_what_is_no_error_class_keeps_its_place():
    # "Exc" is no ending here (pytest's RaisesExc matches errors), and functions, screen parts and
    # classes made from other classes are no error classes
    ff = facts("x.py", cls("RaisesExc", exported=True), fn("handle_error", exported=True),
               Symbol("FieldError", "component", "FieldError({ error })", exported=True),
               err("Mapping", "Generic"), fn("raises", exported=True))
    assert listed(ff) == ["class RaisesExc", "handle_error()", "FieldError({ error })", "class Mapping", "raises()"]


def test_a_built_graph_lists_the_main_class_before_the_errors(tmp_path):
    (tmp_path / "mailer.py").write_text(
        '"""Send mail."""\nimport errors\n\n\n'
        "class MailError(OSError):\n    pass\n\n\n"
        "class Disconnected(MailError):\n    pass\n\n\n"
        "class Cancelled(KeyboardInterrupt):\n    pass\n\n\n"
        "class Refused(errors.SendError):\n    pass\n\n\n"
        "def quote(addr):\n    return addr\n\n\n"
        "class Mailer:\n    \"\"\"Talks to a mail server.\"\"\"\n", encoding="utf-8")
    f = build(str(tmp_path))["nodes"]["mailer.py"]["f"]
    assert f == [["quote(addr)", ""], ["class Mailer", "Talks to a mail server."], ["class MailError", ""],
                 ["class Disconnected", ""], ["class Cancelled", ""], ["class Refused", ""]]


@pytest.mark.parametrize("tree_sitter", [True, False])
def test_javascript_error_classes_come_after_the_rest(tmp_path, monkeypatch, tree_sitter):
    from codevision.lang import js_lang

    if tree_sitter and not js_lang.TREE_SITTER:
        pytest.skip("tree-sitter is not installed")
    monkeypatch.setattr(js_lang, "TREE_SITTER", tree_sitter)
    (tmp_path / "api.ts").write_text(
        "export class ApiError extends Error {}\n"
        "export class TodoClient {}\n"
        "export function getTodos() {\n  return fetch('/api/todos');\n}\n", encoding="utf-8")
    f = build(str(tmp_path))["nodes"]["api.ts"]["f"]
    assert [sig for sig, _ in f] == ["class TodoClient", "getTodos()", "class ApiError"]


@pytest.mark.parametrize("tree_sitter", [True, False])
def test_javascript_classes_made_from_error_classes_come_after_the_rest(tmp_path, monkeypatch, tree_sitter):
    # 2026-11-21: JavaScript classes were known by their names alone, so class NotFound extends HttpError
    # stayed among the main names. Now as in Python: what they extend (Problem is one by Error, so Gone is one).
    from codevision.lang import js_lang

    if tree_sitter and not js_lang.TREE_SITTER:
        pytest.skip("tree-sitter is not installed")
    monkeypatch.setattr(js_lang, "TREE_SITTER", tree_sitter)
    (tmp_path / "errors.ts").write_text(
        "export class HttpError extends Error {}\n"
        "export class NotFound extends HttpError {}\n"
        "export class Problem extends Error {}\n"
        "export class Gone extends Problem {}\n"
        "export class Unauthorized extends errors.ApiError {}\n"
        "export class Api extends Base {}\n"
        "export function get(path: string) {\n  return fetch(path);\n}\n", encoding="utf-8")
    f = build(str(tmp_path))["nodes"]["errors.ts"]["f"]
    assert [sig for sig, _ in f] == ["class Api", "get(path: string)", "class HttpError", "class NotFound",
                                     "class Problem", "class Gone", "class Unauthorized"]
