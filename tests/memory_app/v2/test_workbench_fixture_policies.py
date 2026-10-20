"""共享工厂使用当前策略，历史问答夹具必须显式选择且不得隐式转发。"""
import ast
import symtable
from contextlib import contextmanager
from importlib.util import resolve_name
from pathlib import Path

import pytest

from backend.memory_app.v2.policies import ACTIVE, override, version
from tests.memory_app.v2 import test_workbench_ask as fixtures
from tests.memory_app.v2.test_workbench_ask import env as default_env


PROVIDER = 'tests.memory_app.v2.test_workbench_ask'


def implicit_legacy_imports(sources):
    """按词法作用域追踪静态导入及赋值，拒绝隐式转发历史夹具。"""
    packages = {module.removesuffix('.__init__') for module in sources
                if module.endswith('.__init__')}
    module_sources = {module.removesuffix('.__init__'): source for module, source in sources.items()}
    trees = {module: ast.parse(source) for module, source in module_sources.items()}
    tables = {module: symtable.symtable(source, module, 'exec')
              for module, source in module_sources.items()}
    definition_tables = {}

    def index_tables(module, table, parent=None):
        definition_tables[module, table.get_type(), table.get_name(), table.get_lineno()] = (table, parent)
        for child in table.get_children():
            index_tables(module, child, table)

    for module, table in tables.items():
        index_tables(module, table)
    known_modules = {'.'.join(parts[:index]) for module in (*trees, PROVIDER)
                     for parts in [module.split('.')] for index in range(1, len(parts) + 1)}
    scopes = {}
    counters = {}
    boundaries = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)

    def scope_nodes(nodes):
        for node in nodes:
            yield node
            if not isinstance(node, boundaries):
                yield from scope_nodes(ast.iter_child_nodes(node))

    def add_scope(module, body, table, parent=None, kind='module', private_class=''):
        key = (module, counters.get(module, 0))
        counters[module] = key[1] + 1
        nodes = list(scope_nodes(body))
        # 由编译器区分推导式目标和外层真实绑定，也覆盖异常别名、模式捕获及参数。
        bound = {symbol.get_name() for symbol in table.get_symbols() if symbol.is_local()}
        global_names = {symbol.get_name() for symbol in table.get_symbols() if symbol.is_global()}
        scopes[key] = {'module': module, 'parent': parent, 'bound': bound, 'kind': kind,
                       'globals': global_names, 'private_class': private_class,
                       'body': body, 'table': table,
                       'nodes': [node for node in nodes if isinstance(
                           node, (ast.Import, ast.ImportFrom, ast.Assign, ast.AnnAssign))]}
        for node in nodes:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                # 方法及嵌套类不继承外层类的命名空间。
                lexical_parent = key
                while scopes[lexical_parent]['kind'] == 'class':
                    lexical_parent = scopes[lexical_parent]['parent']
                child_kind = 'class' if isinstance(node, ast.ClassDef) else 'function'
                child_private_class = node.name if isinstance(node, ast.ClassDef) else private_class
                child_table, annotation_table = definition_tables[
                    module, child_kind, node.name, node.lineno]
                if node.type_params:
                    # 类型参数有独立的注解作用域，方法跳过类体后仍可闭包读取这些参数。
                    lexical_parent = add_scope(module, [], annotation_table, lexical_parent,
                                               annotation_table.get_type(), child_private_class)
                add_scope(module, node.body, child_table, lexical_parent, child_kind,
                          child_private_class)
        return key

    for module, tree in trees.items():
        add_scope(module, tree.body, tables[module])
    origins = {((PROVIDER, 0), 'legacy_retrieve_env'): True}
    modules = {}

    def compiled_name(scope, name):
        # 私有名在出现处按当前类改写一次；属性和导入成员也遵循同一规则。
        private_class = scopes[scope]['private_class'].lstrip('_')
        if private_class and name.startswith('__') and not name.endswith('__') and '.' not in name:
            return f'_{private_class}{name}'
        return name

    def imported_module(scope, node):
        # 含点的模块路径不改写，无点的绝对及相对模块名仍需改写。
        name = compiled_name(scope, node.module or '')
        if not node.level:
            return name
        module = scopes[scope]['module']
        package = module if module in packages else module.rpartition('.')[0]
        return resolve_name('.' * node.level + name, package)

    def executes_at_import(scope):
        while scopes[scope]['kind'] not in ('module', 'function'):
            scope = scopes[scope]['parent']
        return scopes[scope]['kind'] == 'module'

    def references_all(node):
        if isinstance(node, ast.Name):
            return node.id == '__all__'
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            return any((alias.asname or alias.name.split('.')[0]) == '__all__'
                       for alias in node.names)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            return node.name == '__all__'
        if isinstance(node, (ast.ExceptHandler, ast.MatchAs, ast.MatchStar)):
            return node.name == '__all__'
        return isinstance(node, ast.MatchMapping) and node.rest == '__all__'

    star_sources = {}
    star_consumers = set()
    for scope, data in scopes.items():
        for node in data['nodes']:
            if isinstance(node, ast.ImportFrom) and any(alias.name == '*' for alias in node.names):
                module = imported_module(scope, node)
                if module not in trees:
                    raise ValueError(f'{data["module"]}:{node.lineno}: 星号来源缺少源码 {module}')
                star_sources[module] = None
                star_consumers.add(data['module'])

    for module in star_sources:
        declaration = None
        allowed_names = set()
        postponed_annotations = any(isinstance(node, ast.ImportFrom) and node.module == '__future__'
                                    and any(alias.name == 'annotations' for alias in node.names)
                                    for node in trees[module].body)
        for node in trees[module].body:
            targets = (node.targets if isinstance(node, ast.Assign)
                       else [node.target] if isinstance(node, ast.AnnAssign) else [])
            if not any(isinstance(target, ast.Name) and target.id == '__all__' for target in targets):
                continue
            if isinstance(node, ast.AnnAssign) and node.value is None:
                allowed_names.add(id(node.target))
                continue
            if declaration is not None:
                raise ValueError(f'{module}:{node.lineno}: 无法静态确定多次赋值的 __all__')
            if (len(targets) != 1 or not isinstance(node.value, (ast.List, ast.Tuple))
                    or not all(isinstance(item, ast.Constant) and isinstance(item.value, str)
                               for item in node.value.elts)):
                raise ValueError(f'{module}:{node.lineno}: __all__ 必须是固定字符串 list 或 tuple')
            declaration = node
            allowed_names.add(id(targets[0]))
            star_sources[module] = tuple(ast.literal_eval(node.value))
        # 只核验导入时执行的模块和类体，未执行函数体保留既有分析边界。
        for scope, data in scopes.items():
            if (data['module'] != module or data['kind'] not in ('module', 'class')
                    or not executes_at_import(scope)):
                continue
            nodes = list(scope_nodes(data['body']))
            local_events = []
            if data['kind'] == 'class':
                annotation_targets = {id(node.target) for node in nodes
                                      if isinstance(node, ast.AnnAssign) and node.value is None}
                for node in nodes:
                    if (references_all(node) and id(node) not in annotation_targets
                            and not (isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load))):
                        local_events.append(((node.lineno, node.col_offset), False))
                for node in data['body']:
                    targets = (node.targets if isinstance(node, ast.Assign)
                               else [node.target] if isinstance(node, ast.AnnAssign) else [])
                    if (targets and isinstance(node.value, (ast.List, ast.Tuple))
                            and any(isinstance(target, ast.Name) and target.id == '__all__'
                                    for target in targets)):
                        local_events.append(((node.end_lineno, node.end_col_offset), True))
                local_events.sort()

            def check_export_reference(node, reference_table=None):
                if not references_all(node) or id(node) in allowed_names:
                    return
                if (reference_table is not None
                        and '__all__' in reference_table.get_identifiers()
                        and not reference_table.lookup('__all__').is_global()):
                    return
                symbol = (data['table'].lookup('__all__')
                          if '__all__' in data['table'].get_identifiers() else None)
                if data['kind'] == 'class' and symbol is not None and not symbol.is_global():
                    if not symbol.is_local():
                        return
                    if not (isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)):
                        return
                    prior = [isolated for position, isolated in local_events
                             if position < (node.lineno, node.col_offset)]
                    if prior and prior[-1]:
                        return
                raise ValueError(f'{module}:{node.lineno}: 无法静态确定 __all__ 的写入或引用')

            for node in nodes:
                check_export_reference(node)
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    continue
                headers = list(node.decorator_list)
                annotations = []
                annotation_table = None
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    headers.extend(node.args.defaults)
                    headers.extend(value for value in node.args.kw_defaults if value is not None)
                    if not postponed_annotations:
                        arguments = [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]
                        arguments.extend(argument for argument in (node.args.vararg, node.args.kwarg)
                                         if argument is not None)
                        annotations.extend(argument.annotation for argument in arguments
                                           if argument.annotation is not None)
                        if node.returns is not None:
                            annotations.append(node.returns)
                        if node.type_params:
                            annotation_table = definition_tables[
                                module, 'function', node.name, node.lineno][1]
                else:
                    annotation_table = definition_tables[module, 'class', node.name, node.lineno][1]
                    shadows_all = (node.type_params and '__all__' in annotation_table.get_identifiers()
                                   and annotation_table.lookup('__all__').is_local())
                    if not shadows_all:
                        headers.extend(node.bases)
                        headers.extend(keyword.value for keyword in node.keywords)
                # 默认值、装饰器和类头部会在定义时执行，参数名本身不读取模块导出表。
                for header in scope_nodes(headers):
                    check_export_reference(header)
                # 普通注解立即求值；泛型注解保留类型参数作用域，future 注解保持延迟。
                for annotation in scope_nodes(annotations):
                    check_export_reference(annotation, annotation_table)

    module_names = {}
    attribute_names = {}

    def imported_names(scope, node):
        if node.names[0].name != '*':
            return [(compiled_name(scope, alias.name), compiled_name(scope, alias.asname or alias.name),
                     alias.name) for alias in node.names]
        module = imported_module(scope, node)
        names = star_sources[module]
        if names is None:
            names = sorted(name for name in module_names[module] if not name.startswith('_'))
        if '__all__' in names and scopes[scope]['module'] in star_sources:
            raise ValueError(f'{scopes[scope]["module"]}:{node.lineno}: '
                             '无法静态确定星号转发的 __all__')
        return [(name, name, name) for name in dict.fromkeys(names)]

    def known_submodule(module, name):
        target = f'{module}.{name}'
        if (target in known_modules
                and (not star_sources or name not in attribute_names.get(module, ()))):
            return target
        return None

    if star_sources:
        module_names = {module: set(scopes[module, 0]['bound']) for module in trees}
        attribute_names = {module: set(names) for module, names in module_names.items()}
        module_imports = {module: [] for module in trees}
        for scope, data in scopes.items():
            own_requests = set()
            independent_bindings = set()
            for node in data['nodes']:
                if (isinstance(node, (ast.Import, ast.ImportFrom))
                        and executes_at_import(scope)):
                    module_imports[data['module']].append((scope, node))
                if data['kind'] != 'module':
                    continue
                if isinstance(node, ast.Import):
                    independent_bindings.update(compiled_name(scope, alias.asname or alias.name.split('.')[0])
                                                for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.names[0].name != '*':
                    base = imported_module(scope, node)
                    for alias in node.names:
                        name = compiled_name(scope, alias.name)
                        bound = compiled_name(scope, alias.asname or alias.name)
                        if base == data['module'] and name == bound:
                            own_requests.add(bound)
                        else:
                            independent_bindings.add(bound)
            if data['kind'] == 'module':
                # 包内 from . import child 仍需加载 child；独立别名及赋值优先于同名子模块。
                for name in own_requests - independent_bindings:
                    symbol = tables[data['module']].lookup(name)
                    if not symbol.is_assigned() and not symbol.is_namespace():
                        attribute_names[data['module']].discard(name)

        def expand_star_names():
            changed = True
            while changed:
                changed = False
                for scope, data in scopes.items():
                    if data['kind'] != 'module':
                        continue
                    for node in data['nodes']:
                        if isinstance(node, ast.ImportFrom) and node.names[0].name == '*':
                            names = {bound for _, bound, _ in imported_names(scope, node)}
                            added = names - module_names[data['module']]
                            attribute_names[data['module']].update(names)
                            if added:
                                module_names[data['module']].update(added)
                                changed = True

        # 名称先收敛，防止反序传播时把尚未解析的别名误判为同名子模块。
        expand_star_names()
        pending_modules = list(star_consumers)
        loaded_modules = set()
        visited_modules = set()

        def load_module(module):
            parts = module.split('.')
            for index in range(1, len(parts) + 1):
                target = '.'.join(parts[:index])
                if target in loaded_modules:
                    continue
                loaded_modules.add(target)
                pending_modules.append(target)
                parent, _, name = target.rpartition('.')
                if parent in packages:
                    module_names[parent].add(name)
                    attribute_names[parent].add(name)
                    modules.setdefault(((parent, 0), name), set()).add(target)

        # 消费者开始执行前，Python 已经完成其父包的初始化。
        for consumer in star_consumers:
            package = consumer if consumer in packages else consumer.rpartition('.')[0]
            if package:
                load_module(package)

        # 真实导入会把子模块写入父包；仅沿星号消费者的导入闭包建立这些属性。
        while pending_modules:
            module = pending_modules.pop()
            if module in visited_modules or module not in module_imports:
                continue
            visited_modules.add(module)
            for scope, node in module_imports[module]:
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        load_module(compiled_name(scope, alias.name))
                else:
                    base = imported_module(scope, node)
                    load_module(base)
                    names = (star_sources[base] or () if node.names[0].name == '*'
                             else [compiled_name(scope, alias.name) for alias in node.names])
                    for name in names:
                        target = known_submodule(base, name)
                        if target:
                            load_module(target)
        expand_star_names()

    def visible(scope, name, bindings):
        while scope is not None:
            if (scope, name) in bindings:
                return bindings[scope, name]
            # 内联推导式会在父域留下暂存局部，global 引用应直接读取模块来源。
            if scopes[scope]['kind'] != 'module' and name in scopes[scope]['globals']:
                scope = (scopes[scope]['module'], 0)
                continue
            if name in scopes[scope]['bound']:
                return None
            scope = scopes[scope]['parent']
        return None

    def module_values(scope, value):
        if isinstance(value, ast.Name):
            return visible(scope, compiled_name(scope, value.id), modules) or set()
        if isinstance(value, ast.Attribute):
            targets = set()
            name = compiled_name(scope, value.attr)
            for module in module_values(scope, value.value):
                targets.update(modules.get(((module, 0), name), ()))
                target = known_submodule(module, name)
                if target:
                    targets.add(target)
            return targets
        return set()

    def origin(scope, value):
        if isinstance(value, ast.Name):
            return visible(scope, compiled_name(scope, value.id), origins)
        if isinstance(value, ast.Attribute):
            return any(origins.get(((module, 0), compiled_name(scope, value.attr)))
                       for module in module_values(scope, value.value))
        return False

    changed = True
    while changed:
        changed = False
        for scope, data in scopes.items():
            for node in data['nodes']:
                bindings = []
                if isinstance(node, ast.Import):
                    bindings = [(compiled_name(scope, alias.asname or alias.name.split('.')[0]), False,
                                 {compiled_name(scope, alias.name) if alias.asname
                                  else compiled_name(scope, alias.name).split('.')[0]})
                                for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    module = imported_module(scope, node)
                    for name, bound_name, _ in imported_names(scope, node):
                        target = known_submodule(module, name)
                        targets = set(modules.get(((module, 0), name), ()))
                        if target:
                            targets.add(target)
                        bindings.append((bound_name, origins.get(((module, 0), name)), targets))
                elif node.value is not None:
                    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                    bindings = [(compiled_name(scope, target.id), origin(scope, node.value),
                                 module_values(scope, node.value))
                                for target in targets if isinstance(target, ast.Name)]
                for name, selected, targets in bindings:
                    if selected and (scope, name) not in origins:
                        origins[scope, name] = True
                        changed = True
                    added = targets - modules.get((scope, name), set())
                    if added:
                        modules.setdefault((scope, name), set()).update(added)
                        changed = True
    violations = []
    for scope, data in scopes.items():
        for node in data['nodes']:
            if isinstance(node, ast.ImportFrom):
                module = imported_module(scope, node)
                for name, _, display_name in imported_names(scope, node):
                    if origins.get(((module, 0), name)) and display_name != 'legacy_retrieve_env':
                        violations.append((data['module'], node.lineno, display_name))
            elif isinstance(node, (ast.Assign, ast.AnnAssign)) and isinstance(node.value, ast.Attribute):
                if origin(scope, node.value) and node.value.attr != 'legacy_retrieve_env':
                    violations.append((data['module'], node.lineno, node.value.attr))
    return violations


def test_compatibility_fixture_is_not_implicitly_reexported():
    root = Path(__file__).resolve().parents[3]
    sources = {'.'.join(path.relative_to(root).with_suffix('').parts):
               path.read_text(encoding='utf-8-sig') for path in (root / 'tests').rglob('*.py')}
    assert implicit_legacy_imports(sources) == []


@pytest.mark.parametrize('transit', [
    'from tests.consumer import env as hidden',
    'import tests.consumer as shared\nhidden = shared.env',
    'from tests import consumer as shared\nhidden = shared.env',
])
def test_fixture_import_gate_rejects_hidden_compatibility_alias(transit):
    sources = {
        'tests.consumer': f'from {PROVIDER} import legacy_retrieve_env as _legacy\nenv = _legacy',
        'tests.future': transit,
    }
    line = 2 if '\n' in transit else 1
    assert implicit_legacy_imports(sources) == [('tests.future', line, 'env')]


def test_fixture_import_gate_accepts_explicit_compatibility_and_current_default():
    sources = {
        'tests.consumer': f'from {PROVIDER} import legacy_retrieve_env as env',
        'tests.future': f'from {PROVIDER} import env\nfrom tests.consumer import publish',
    }
    assert implicit_legacy_imports(sources) == []


@pytest.mark.parametrize('transit', [
    'from .consumer import env as hidden',
    'import tests.consumer as shared\nhidden: object = shared.env',
])
def test_fixture_import_gate_rejects_relative_and_annotated_consumption(transit):
    sources = {
        'tests.consumer': f'from {PROVIDER} import legacy_retrieve_env as _legacy\nenv = _legacy',
        'tests.future': transit,
    }
    line = 2 if '\n' in transit else 1
    assert implicit_legacy_imports(sources) == [('tests.future', line, 'env')]


@pytest.mark.parametrize('declaration', [
    'if True:\n    {export}',
    'try:\n    {export}\nexcept ImportError:\n    pass',
    'try:\n    pass\nexcept ImportError:\n    {export}',
    'with manager():\n    {export}',
    'for _ in values:\n    {export}',
    'while condition:\n    {export}',
    'match value:\n    case True:\n        {export}',
])
def test_fixture_import_gate_tracks_a_conditional_module_export(declaration):
    sources = {
        'tests.consumer': declaration.format(export=f'from {PROVIDER} import legacy_retrieve_env as env'),
        'tests.future': 'from tests.consumer import env',
    }
    assert implicit_legacy_imports(sources) == [('tests.future', 1, 'env')]


@pytest.mark.parametrize('consumer, future, statement', [
    ('tests.consumer', 'tests.future', 'from . import consumer as shared\nhidden = shared.env'),
    ('tests.consumer', 'tests.nested.future', 'from ..consumer import env as hidden'),
    ('tests.nested.consumer', 'tests.future', 'import tests.nested.consumer\nhidden = tests.nested.consumer.env'),
])
def test_fixture_import_gate_resolves_relative_and_namespace_module_aliases(consumer, future, statement):
    sources = {
        consumer: f'from {PROVIDER} import legacy_retrieve_env as env',
        future: statement,
    }
    line = 2 if '\n' in statement else 1
    assert implicit_legacy_imports(sources) == [(future, line, 'env')]


def test_fixture_import_gate_resolves_package_initializers_and_annotated_exports():
    sources = {
        'tests.consumer.__init__': 'from ..memory_app.v2.test_workbench_ask import legacy_retrieve_env as _legacy\n'
                                   'env: object = _legacy',
        'tests.future': 'from .consumer import env',
    }
    assert implicit_legacy_imports(sources) == [('tests.future', 1, 'env')]


def test_fixture_import_gate_tracks_multilevel_module_and_fixture_aliases():
    sources = {
        'tests.future': 'from .middle import exported\nhidden: object = exported.env',
        'tests.middle': 'from . import consumer as shared\nexported: object = shared',
        'tests.consumer': f'from {PROVIDER} import legacy_retrieve_env as _legacy\nenv: object = _legacy',
        'tests.later': 'from .future import hidden',
    }
    assert implicit_legacy_imports(sources) == [('tests.future', 2, 'env'), ('tests.later', 1, 'hidden')]


@pytest.mark.parametrize('scope', ['def local():', 'async def local():', 'class Local:'])
def test_fixture_import_gate_keeps_local_fixture_bindings_out_of_module_exports(scope):
    sources = {
        'tests.future': f'from {PROVIDER} import env\n{scope}\n'
                        f'    from {PROVIDER} import legacy_retrieve_env as env',
        'tests.later': 'from tests.future import env',
    }
    assert implicit_legacy_imports(sources) == []


@pytest.mark.parametrize('scope', ['def local():', 'async def local():', 'class Local:'])
def test_fixture_import_gate_keeps_local_module_aliases_out_of_module_exports(scope):
    sources = {
        'tests.consumer': f'from {PROVIDER} import legacy_retrieve_env as env',
        'tests.current': f'from {PROVIDER} import env',
        'tests.future': 'import tests.current as shared\n'
                        f'from {PROVIDER} import env\n{scope}\n'
                        '    import tests.consumer as shared\n'
                        f'    from {PROVIDER} import legacy_retrieve_env as env\n'
                        'current: object = shared.env',
        'tests.later': 'from tests.future import env, current',
    }
    assert implicit_legacy_imports(sources) == []


def test_fixture_import_gate_rejects_local_implicit_consumption_without_exporting_it():
    sources = {
        'tests.consumer': f'from {PROVIDER} import legacy_retrieve_env as env',
        'tests.future': f'from {PROVIDER} import env\ndef local():\n'
                        '    import tests.consumer as shared\n    env: object = shared.env',
        'tests.later': 'from tests.future import env',
    }
    assert implicit_legacy_imports(sources) == [('tests.future', 4, 'env')]


@pytest.mark.parametrize('statement', [
    'def local():\n    from tests.consumer import env',
    'async def local():\n    from .consumer import env',
    'class Local:\n    from . import consumer as shared\n    env: object = shared.env',
])
def test_fixture_import_gate_rejects_local_implicit_imports(statement):
    sources = {
        'tests.consumer': f'from {PROVIDER} import legacy_retrieve_env as env',
        'tests.future': statement,
    }
    line = 3 if 'class Local:' in statement else 2
    assert implicit_legacy_imports(sources) == [('tests.future', line, 'env')]


def test_fixture_import_gate_accepts_relative_explicit_and_generic_selection():
    sources = {
        'tests.future.__init__': 'from ..memory_app.v2.test_workbench_ask import legacy_retrieve_env',
        'tests.current': 'from .memory_app.v2.test_workbench_ask import env',
        'tests.later': 'from .future import legacy_retrieve_env\nfrom .current import env',
    }
    assert implicit_legacy_imports(sources) == []


def test_fixture_import_gate_accepts_conditional_and_annotated_current_selection():
    sources = {
        'tests.consumer': f'if True:\n    from {PROVIDER} import env',
        'tests.future': 'from . import consumer as shared\nhidden: object = shared.env',
    }
    assert implicit_legacy_imports(sources) == []


def test_fixture_import_gate_respects_local_parameter_shadowing():
    sources = {
        'tests.consumer': f'from {PROVIDER} import legacy_retrieve_env as env',
        'tests.future': 'import tests.consumer as shared\ndef local(shared):\n    env = shared.env',
    }
    assert implicit_legacy_imports(sources) == []


@pytest.mark.parametrize('comprehension', [
    '[shared for shared in []]',
    '{shared for shared in []}',
    '{shared: shared for shared in []}',
    '(shared for shared in [])',
    '[[shared for shared in []] for shared in []]',
])
@pytest.mark.parametrize('selection, attribute, rejected', [
    pytest.param('legacy_retrieve_env as env', 'env', True, id='implicit-history'),
    pytest.param('env', 'env', False, id='current-default'),
    pytest.param('legacy_retrieve_env', 'legacy_retrieve_env', False, id='explicit-history'),
])
def test_fixture_import_gate_preserves_outer_source_after_comprehension(
        comprehension, selection, attribute, rejected):
    sources = {
        'tests.consumer': f'from {PROVIDER} import {selection}',
        'tests.future': 'import tests.consumer as shared\ndef local():\n'
                        f'    values = {comprehension}\n    hidden = shared.{attribute}',
    }
    expected = [('tests.future', 4, 'env')] if rejected else []
    assert implicit_legacy_imports(sources) == expected


@pytest.mark.parametrize('comprehension', [
    '[(shared := item) for item in []]',
    '((shared := item) for item in [])',
])
def test_fixture_import_gate_preserves_comprehension_walrus_outer_binding(comprehension):
    sources = {
        'tests.consumer': f'from {PROVIDER} import legacy_retrieve_env as env',
        'tests.future': 'import tests.consumer as shared\ndef local():\n'
                        f'    values = {comprehension}\n    hidden = shared.env',
    }
    assert implicit_legacy_imports(sources) == []


@pytest.mark.parametrize('handler', ['except', 'except*'])
@pytest.mark.parametrize('alias', ['shared', 'other'])
def test_fixture_import_gate_respects_exception_alias_binding(handler, alias):
    sources = {
        'tests.consumer': f'from {PROVIDER} import legacy_retrieve_env as env',
        'tests.future': 'import tests.consumer as shared\ndef local():\n'
                        '    try:\n        fail()\n'
                        f'    {handler} RuntimeError as {alias}:\n        hidden = shared.env',
    }
    expected = [] if alias == 'shared' else [('tests.future', 6, 'env')]
    assert implicit_legacy_imports(sources) == expected


@pytest.mark.parametrize('pattern, shadows_module', [
    pytest.param('shared', True, id='capture'),
    pytest.param('other as shared', True, id='as-capture'),
    pytest.param('[head, *shared]', True, id='star-capture'),
    pytest.param('{**shared}', True, id='mapping-rest'),
    pytest.param('{"module": [head, *shared]}', True, id='nested-capture'),
    pytest.param('Namespace(value=shared)', True, id='class-capture'),
    pytest.param('[shared] | (shared,)', True, id='or-capture'),
    pytest.param('None', False, id='no-capture'),
    pytest.param('{"module": other}', False, id='different-capture'),
])
def test_fixture_import_gate_respects_match_capture_binding(pattern, shadows_module):
    sources = {
        'tests.consumer': f'from {PROVIDER} import legacy_retrieve_env as env',
        'tests.future': 'import tests.consumer as shared\ndef local():\n'
                        f'    match value:\n        case {pattern}:\n            hidden = shared.env',
    }
    expected = [] if shadows_module else [('tests.future', 5, 'env')]
    assert implicit_legacy_imports(sources) == expected


@pytest.mark.parametrize('scope, line', [
    pytest.param('class Local:\n    values = [shared for shared in []]\n    hidden = shared.env',
                 4, id='class-body'),
    pytest.param('class Local:\n    shared = object()\n    def method(self):\n'
                 '        values = [shared for shared in []]\n        hidden = shared.env',
                 6, id='method-skips-class'),
    pytest.param('def outer():\n    shared = object()\n    def inner():\n'
                 '        values = [shared for shared in []]\n        hidden = shared.env',
                 None, id='closure-keeps-function-binding'),
    pytest.param('def outer():\n    values = [shared for shared in []]\n    def inner():\n'
                 '        values = [shared for shared in []]\n        hidden = shared.env',
                 6, id='closure-keeps-module-source'),
])
def test_fixture_import_gate_keeps_comprehension_scope_boundaries(scope, line):
    sources = {
        'tests.consumer': f'from {PROVIDER} import legacy_retrieve_env as env',
        'tests.future': f'import tests.consumer as shared\n{scope}',
    }
    expected = [('tests.future', line, 'env')] if line else []
    assert implicit_legacy_imports(sources) == expected


@pytest.mark.parametrize('scope', ['def local[T]():', 'class Local[T]:'])
def test_fixture_import_gate_finds_generic_scope_binding(scope):
    sources = {
        'tests.consumer': f'from {PROVIDER} import legacy_retrieve_env as env',
        'tests.future': f'import tests.consumer as shared\n{scope}\n'
                        '    match value:\n        case shared:\n            hidden = shared.env',
    }
    assert implicit_legacy_imports(sources) == []


@pytest.mark.parametrize('scope', [
    'class Local:\n    __shared = object()\n    hidden = __shared.env',
    'class _Local:\n    def method(self, __shared):\n        hidden = __shared.env',
])
def test_fixture_import_gate_preserves_private_class_binding(scope):
    sources = {
        'tests.consumer': f'from {PROVIDER} import legacy_retrieve_env as env',
        'tests.future': f'import tests.consumer as __shared\n{scope}',
    }
    assert implicit_legacy_imports(sources) == []


@pytest.mark.parametrize('declaration', [
    'def local[{parameter}]():',
    'async def local[{parameter}]():',
    'class Local[{parameter}]:',
])
@pytest.mark.parametrize('parameter', ['shared', '*shared', '**shared'])
@pytest.mark.parametrize('shadows_module', [True, False], ids=['same-name', 'different-name'])
def test_fixture_import_gate_respects_type_parameter_binding(declaration, parameter, shadows_module):
    selected = parameter if shadows_module else parameter.replace('shared', 'Other')
    sources = {
        'tests.consumer': f'from {PROVIDER} import legacy_retrieve_env as env',
        'tests.future': 'import tests.consumer as shared\n'
                        f'{declaration.format(parameter=selected)}\n    hidden = shared.env',
    }
    expected = [] if shadows_module else [('tests.future', 3, 'env')]
    assert implicit_legacy_imports(sources) == expected


@pytest.mark.parametrize('declaration, line', [
    pytest.param('def outer[{parameter}]():\n    def inner():\n        hidden = shared.env',
                 4, id='nested-function'),
    pytest.param('def outer[{parameter}]():\n    def inner[T]():\n        hidden = shared.env',
                 4, id='nested-generic-function'),
    pytest.param('class Outer[{parameter}]:\n    shared = object()\n    def method(self):\n'
                 '        hidden = shared.env', 5, id='method-skips-class-binding'),
    pytest.param('class Outer[{parameter}]:\n    class Inner:\n        def method(self):\n'
                 '            hidden = shared.env', 5, id='nested-class-method'),
    pytest.param('def outer[{parameter}]():\n    class Inner:\n        def method(self):\n'
                 '            hidden = shared.env', 5, id='function-class-closure'),
    pytest.param('class Outer:\n    shared = object()\n    def method[{parameter}](self):\n'
                 '        hidden = shared.env', 5, id='generic-method'),
])
@pytest.mark.parametrize('parameter', ['shared', 'Other'])
def test_fixture_import_gate_keeps_type_parameter_closures(declaration, line, parameter):
    sources = {
        'tests.consumer': f'from {PROVIDER} import legacy_retrieve_env as env',
        'tests.future': 'import tests.consumer as shared\n' + declaration.format(parameter=parameter),
    }
    expected = [] if parameter == 'shared' else [('tests.future', line, 'env')]
    assert implicit_legacy_imports(sources) == expected


@pytest.mark.parametrize('declaration', [
    'def local[shared]():', 'async def local[shared]():', 'class Local[shared]:',
])
def test_fixture_import_gate_keeps_global_lookup_past_type_parameter(declaration):
    sources = {
        'tests.consumer': f'from {PROVIDER} import legacy_retrieve_env as env',
        'tests.future': f'import tests.consumer as shared\n{declaration}\n'
                        '    global shared\n    hidden = shared.env',
    }
    assert implicit_legacy_imports(sources) == [('tests.future', 4, 'env')]


@pytest.mark.parametrize('declaration', [
    'def local[shared]():', 'async def local[shared]():', 'class Local[shared]:',
])
@pytest.mark.parametrize('binding', ['import tests.consumer as shared', 'shared = original'])
def test_fixture_import_gate_rejects_real_module_binding_over_type_parameter(declaration, binding):
    sources = {
        'tests.consumer': f'from {PROVIDER} import legacy_retrieve_env as env',
        'tests.future': f'import tests.consumer as original\n{declaration}\n'
                        f'    {binding}\n    hidden = shared.env',
    }
    assert implicit_legacy_imports(sources) == [('tests.future', 4, 'env')]


@pytest.mark.parametrize('declaration', [
    'def local[shared]():', 'async def local[shared]():', 'class Local[shared]:',
])
def test_fixture_import_gate_keeps_type_parameter_out_of_outer_scope(declaration):
    sources = {
        'tests.consumer': f'from {PROVIDER} import legacy_retrieve_env as env',
        'tests.future': f'import tests.consumer as shared\n{declaration}\n'
                        '    hidden = shared.env\noutside = shared.env',
    }
    assert implicit_legacy_imports(sources) == [('tests.future', 4, 'env')]


@pytest.mark.parametrize('declaration', [
    'class Local[__shared]:\n    hidden = __shared.env',
    'class Local:\n    def method[__shared](self):\n        hidden = __shared.env',
])
def test_fixture_import_gate_preserves_private_type_parameter_binding(declaration):
    sources = {
        'tests.consumer': f'from {PROVIDER} import legacy_retrieve_env as env',
        'tests.future': f'import tests.consumer as __shared\n{declaration}',
    }
    assert implicit_legacy_imports(sources) == []


@pytest.mark.parametrize('class_name, prefix', [
    ('Local', '_Local'), ('_Local', '_Local'), ('__Local', '_Local'), ('___', ''),
])
@pytest.mark.parametrize('selection, rejected', [
    ('legacy_retrieve_env as env', True), ('env', False),
])
def test_fixture_import_gate_tracks_private_module_lookup(class_name, prefix, selection, rejected):
    sources = {
        'tests.consumer': f'from {PROVIDER} import {selection}',
        'tests.future': f'import tests.consumer as {prefix}__shared\nclass {class_name}:\n'
                        '    hidden = __shared.env',
    }
    expected = [('tests.future', 3, 'env')] if rejected else []
    assert implicit_legacy_imports(sources) == expected


@pytest.mark.parametrize('declaration, prefix, line', [
    ('class Local:\n    def method(self):\n        hidden = __shared.env', '_Local', 4),
    ('class Local:\n    def method(self):\n        def inner():\n'
     '            hidden = __shared.env', '_Local', 5),
    ('class Outer:\n    class Inner:\n        hidden = __shared.env', '_Inner', 4),
    ('class Outer:\n    class Inner:\n        def method(self):\n'
     '            hidden = __shared.env', '_Inner', 5),
])
@pytest.mark.parametrize('selection, rejected', [
    ('legacy_retrieve_env as env', True), ('env', False),
])
def test_fixture_import_gate_keeps_private_lookup_context(declaration, prefix, line, selection, rejected):
    sources = {
        'tests.consumer': f'from {PROVIDER} import {selection}',
        'tests.future': f'import tests.consumer as {prefix}__shared\n{declaration}',
    }
    expected = [('tests.future', line, 'env')] if rejected else []
    assert implicit_legacy_imports(sources) == expected


@pytest.mark.parametrize('declaration, line', [
    ('class Local:\n    {binding} = object()\n    hidden = __shared.env', 4),
    ('class Local:\n    def method(self, {binding}):\n        hidden = __shared.env', 4),
    ('class Local[{binding}]:\n    hidden = __shared.env', 3),
    ('class Local:\n    def method[{binding}](self):\n        hidden = __shared.env', 4),
])
@pytest.mark.parametrize('binding', ['__shared', '__other'])
def test_fixture_import_gate_respects_mangled_local_shadowing(declaration, line, binding):
    sources = {
        'tests.consumer': f'from {PROVIDER} import legacy_retrieve_env as env',
        'tests.future': 'import tests.consumer as _Local__shared\n'
                        + declaration.format(binding=binding),
    }
    expected = [] if binding == '__shared' else [('tests.future', line, 'env')]
    assert implicit_legacy_imports(sources) == expected


@pytest.mark.parametrize('qualifier, rejected', [('', False), ('nonlocal __shared\n            ', False),
                                              ('global __shared\n            ', True)])
def test_fixture_import_gate_resolves_mangled_global_and_free_names(qualifier, rejected):
    sources = {
        'tests.consumer': f'from {PROVIDER} import legacy_retrieve_env as env',
        'tests.current': f'from {PROVIDER} import env',
        'tests.future': 'import tests.consumer as _Local__shared\ndef outer():\n'
                        '    import tests.current as _Local__shared\n    class Local:\n'
                        '        def method(self):\n'
                        f'            {qualifier}hidden = __shared.env',
    }
    expected = [('tests.future', 7, 'env')] if rejected else []
    assert implicit_legacy_imports(sources) == expected


@pytest.mark.parametrize('attribute, exported', [
    ('__env', '_Local__env'), ('__env__', '__env__'), ('_env', '_env'),
])
@pytest.mark.parametrize('selection, rejected', [
    ('legacy_retrieve_env', True), ('env', False),
])
def test_fixture_import_gate_tracks_private_attribute_names(attribute, exported, selection, rejected):
    sources = {
        'tests.consumer': f'from {PROVIDER} import {selection} as {exported}',
        'tests.future': f'import tests.consumer as shared\nclass Local:\n'
                        f'    hidden = shared.{attribute}',
    }
    expected = [('tests.future', 3, attribute)] if rejected else []
    assert implicit_legacy_imports(sources) == expected


@pytest.mark.parametrize('alias', ['', ' as __bound', ' as selected'])
@pytest.mark.parametrize('selection, rejected', [
    ('legacy_retrieve_env', True), ('env', False),
])
def test_fixture_import_gate_tracks_private_import_members(alias, selection, rejected):
    sources = {
        'tests.consumer': f'from {PROVIDER} import {selection} as _Local__env',
        'tests.future': f'class Local:\n    from tests.consumer import __env{alias}',
    }
    expected = [('tests.future', 2, '__env')] if rejected else []
    assert implicit_legacy_imports(sources) == expected


@pytest.mark.parametrize('binding, reference', [
    ('from tests.middle import __shared', '__shared'),
    ('from tests.middle import __shared as __bound', '__bound'),
    ('from tests.middle import __shared as selected', 'selected'),
    ('import tests.consumer as __shared', '__shared'),
    ('__shared = original', '__shared'),
])
@pytest.mark.parametrize('selection, rejected', [
    ('legacy_retrieve_env as env', True), ('env', False),
])
def test_fixture_import_gate_tracks_private_module_bindings(binding, reference, selection, rejected):
    sources = {
        'tests.consumer': f'from {PROVIDER} import {selection}',
        'tests.middle': 'import tests.consumer as _Local__shared',
        'tests.future': 'import tests.consumer as original\nclass Local:\n'
                        f'    {binding}\n    hidden = {reference}.env',
    }
    expected = [('tests.future', 4, 'env')] if rejected else []
    assert implicit_legacy_imports(sources) == expected


@pytest.mark.parametrize('statement, module, line', [
    ('import __consumer as __shared\n    hidden = __shared.env', '_Local__consumer', 3),
    ('from __consumer import env', '_Local__consumer', 2),
    ('from .__consumer import env', 'tests._Local__consumer', 2),
    ('from . import __consumer as __shared\n    hidden = __shared.env',
     'tests._Local__consumer', 3),
    ('import __pkg.consumer as __shared\n    hidden = __shared.env', '__pkg.consumer', 3),
    ('import __pkg.consumer\n    hidden = __pkg.consumer.env', '__pkg.consumer', 3),
    ('from __pkg.consumer import env', '__pkg.consumer', 2),
])
@pytest.mark.parametrize('selection, rejected', [
    ('legacy_retrieve_env as env', True), ('env', False),
])
def test_fixture_import_gate_mangles_only_simple_import_module_names(
        statement, module, line, selection, rejected):
    sources = {
        module: f'from {PROVIDER} import {selection}',
        'tests.future': f'class Local:\n    {statement}',
    }
    expected = [('tests.future', line, 'env')] if rejected else []
    assert implicit_legacy_imports(sources) == expected


def test_fixture_import_gate_ignores_unmangled_private_decoys():
    sources = {
        'tests.consumer': f'from {PROVIDER} import legacy_retrieve_env as __env\n'
                          f'from {PROVIDER} import env as _Local__env',
        '__consumer': f'from {PROVIDER} import legacy_retrieve_env as env',
        '_Local__consumer': f'from {PROVIDER} import env',
        'tests.future': 'import tests.consumer as shared\nclass Local:\n'
                        '    hidden = shared.__env\n    from tests.consumer import __env\n'
                        '    from __consumer import env',
    }
    assert implicit_legacy_imports(sources) == []


@pytest.mark.parametrize('statement, future', [
    ('from tests.consumer import *', 'tests.future'),
    ('from .consumer import *', 'tests.future'),
    ('from ..consumer import *', 'tests.nested.future'),
    ('from . import *', 'tests.consumer.future'),
])
@pytest.mark.parametrize('selection, rejected', [
    ('legacy_retrieve_env as env', True), ('env', False), ('legacy_retrieve_env', False),
])
def test_fixture_import_gate_expands_default_star_exports(statement, future, selection, rejected):
    consumer = 'tests.consumer.__init__' if statement == 'from . import *' else 'tests.consumer'
    sources = {
        consumer: f'from {PROVIDER} import {selection}',
        future: statement,
    }
    expected = [(future, 1, 'env')] if rejected else []
    assert implicit_legacy_imports(sources) == expected


@pytest.mark.parametrize('export, declaration, selected', [
    ('env', '', True),
    ('_env', '', False),
    ('__env', '', False),
    ('env', "__all__ = ['env']", True),
    ('env', "__all__ = ('env',)", True),
    ('env', '__all__: list[str]', True),
    ('env', '__all__ = []', False),
    ('env', '__all__ = ()', False),
    ('env', "__all__ = ['current']", False),
    ('_env', "__all__ = ['_env']", True),
    ('__env', "__all__: tuple[str, ...] = ('__env',)", True),
])
@pytest.mark.parametrize('selection, rejected', [('legacy_retrieve_env', True), ('env', False)])
def test_fixture_import_gate_respects_fixed_star_export_lists(
        export, declaration, selected, selection, rejected):
    sources = {
        'tests.consumer': f'from {PROVIDER} import {selection} as {export}\n'
                          f'from {PROVIDER} import env as current\n{declaration}',
        'tests.future': 'from tests.consumer import *',
    }
    expected = [('tests.future', 1, export)] if selected and rejected else []
    assert implicit_legacy_imports(sources) == expected


@pytest.mark.parametrize('reverse', [False, True])
@pytest.mark.parametrize('declaration', ['', "\n__all__ = ['env']"])
def test_fixture_import_gate_propagates_star_exports_to_a_fixed_point(reverse, declaration):
    sources = {
        'tests.consumer': f'from {PROVIDER} import legacy_retrieve_env as env{declaration}',
        'tests.middle': 'from .consumer import *',
        'tests.later.__init__': 'from ..middle import *',
        'tests.future': 'from .later import *',
    }
    if reverse:
        sources = dict(reversed(list(sources.items())))
    expected = [('tests.middle', 1, 'env'), ('tests.later', 1, 'env'), ('tests.future', 1, 'env')]
    assert sorted(implicit_legacy_imports(sources)) == sorted(expected)


@pytest.mark.parametrize('declaration', ['', "\n__all__ = ['shared']", "\n__all__ = ('shared',)"])
@pytest.mark.parametrize('selection, rejected', [
    ('legacy_retrieve_env as env', True), ('env', False),
])
def test_fixture_import_gate_tracks_module_aliases_from_star_exports(declaration, selection, rejected):
    sources = {
        'tests.future': 'from tests.middle import *\nhidden = shared.env',
        'tests.middle': f'import tests.consumer as shared{declaration}',
        'tests.consumer': f'from {PROVIDER} import {selection}',
    }
    expected = [('tests.future', 2, 'env')] if rejected else []
    assert implicit_legacy_imports(sources) == expected


def test_fixture_import_gate_retains_explicit_history_name_through_star_exports():
    sources = {
        PROVIDER: 'def legacy_retrieve_env():\n    pass\ndef env():\n    pass',
        'tests.consumer': f'from {PROVIDER} import *',
        'tests.future': 'from tests.consumer import *',
        'tests.later': 'from tests.future import legacy_retrieve_env',
        'tests.renamed': 'from tests.future import legacy_retrieve_env as env',
        'tests.hidden': 'from tests.renamed import *',
    }
    assert implicit_legacy_imports(sources) == [('tests.hidden', 1, 'env')]


@pytest.mark.parametrize('declaration, line', [
    ('__all__ = names()', 2),
    ("__all__ = []\n__all__.append('env')", 3),
    ("__all__ = []\n__all__.extend(['env'])", 3),
    ("__all__ = []\n__all__ += ['env']", 3),
    ('__all__ = []\ndel __all__', 3),
    ("__all__ = ['current']\n__all__[0] = 'env'", 3),
    ("if condition:\n    __all__ = ['env']", 3),
    ("__all__ = ['env']\n__all__ = []", 3),
    ("__all__ = {'env'}", 2),
    ("__all__ = ['env', 7]", 2),
    ("__all__ = 'env'", 2),
    ('from tests.names import __all__', 2),
    ("(__all__ := ['env'])", 2),
    ("for __all__ in values:\n    pass", 2),
    ('__all__ = []\nexports = __all__', 3),
])
def test_fixture_import_gate_rejects_indeterminate_star_exports(declaration, line):
    sources = {
        'tests.consumer': f'from {PROVIDER} import legacy_retrieve_env as env\n{declaration}',
        'tests.future': 'from tests.consumer import *',
    }
    with pytest.raises(ValueError, match=rf'tests\.consumer:{line}:.*__all__'):
        implicit_legacy_imports(sources)


@pytest.mark.parametrize('declaration', [
    '__all__ = names()', "__all__ = []\n__all__.append('env')",
    "__all__ = []\n__all__ += ['env']", '__all__ = []\ndel __all__',
])
def test_fixture_import_gate_validates_export_lists_only_for_star_consumption(declaration):
    sources = {
        'tests.consumer': f'from {PROVIDER} import legacy_retrieve_env as env\n{declaration}',
        'tests.future': 'from tests.consumer import env',
    }
    assert implicit_legacy_imports(sources) == [('tests.future', 1, 'env')]


@pytest.mark.parametrize('statement, missing', [
    ('from tests.missing import *', 'tests.missing'),
    ('from .missing import *', 'tests.missing'),
    ('from . import *', 'tests'),
])
def test_fixture_import_gate_rejects_missing_star_source(statement, missing):
    sources = {'tests.future': statement}
    with pytest.raises(ValueError, match=rf'tests\.future:1:.*{missing}'):
        implicit_legacy_imports(sources)


@pytest.mark.parametrize('declaration', [
    'def local():\n    __all__ = names()',
    'class Local:\n    __all__ = names()',
])
def test_fixture_import_gate_keeps_nested_export_lists_out_of_star_exports(declaration):
    sources = {
        'tests.consumer': f'from {PROVIDER} import legacy_retrieve_env as env\n{declaration}',
        'tests.future': 'from tests.consumer import *',
    }
    assert implicit_legacy_imports(sources) == [('tests.future', 1, 'env')]


@pytest.mark.parametrize('loader', [
    'from .consumer import legacy_retrieve_env',
    'from . import consumer',
    'import tests.package.consumer',
])
@pytest.mark.parametrize('child', ['consumer', 'provider'])
@pytest.mark.parametrize('historical', [False, True])
def test_fixture_import_gate_tracks_loaded_package_children_in_star_exports(loader, child, historical):
    selection = '\nenv = legacy_retrieve_env' if historical else ''
    sources = {
        'tests.package.__init__': loader,
        'tests.package.consumer': f'from .provider import legacy_retrieve_env\n'
                                  f'from {PROVIDER} import env{selection}',
        'tests.package.provider': f'from {PROVIDER} import legacy_retrieve_env, env{selection}',
        'tests.future': f'from tests.package import *\nhidden = {child}.env',
    }
    expected = [('tests.future', 2, 'env')] if historical else []
    assert implicit_legacy_imports(sources) == expected


@pytest.mark.parametrize('loaded', [False, True])
@pytest.mark.parametrize('declaration', ['', '\n__all__ = []', "\n__all__ = ('current',)"])
def test_fixture_import_gate_limits_package_star_exports_to_loaded_and_selected_children(
        loaded, declaration):
    loader = 'from .consumer import legacy_retrieve_env\n' if loaded else ''
    sources = {
        'tests.package.__init__': f'{loader}from {PROVIDER} import env as current{declaration}',
        'tests.package.consumer': 'from .provider import legacy_retrieve_env\nenv = legacy_retrieve_env',
        'tests.package.provider': f'from {PROVIDER} import legacy_retrieve_env',
        'tests.future': "from tests.package import *\nif 'consumer' in globals():\n"
                        '    hidden = consumer.env',
    }
    expected = [('tests.future', 3, 'env')] if loaded and not declaration else []
    assert implicit_legacy_imports(sources) == expected


@pytest.mark.parametrize('declaration, loaded', [
    ('def unused():\n    import tests.package.consumer', False),
    ('async def unused():\n    import tests.package.consumer', False),
    ('class Local:\n    def unused(self):\n        import tests.package.consumer', False),
    ('def unused():\n    class Local:\n        import tests.package.consumer', False),
    ('class Local:\n    import tests.package.consumer as _private', True),
    ('class Local[T]:\n    import tests.package.consumer as _private', True),
])
def test_fixture_import_gate_preserves_execution_scope_for_package_star_exports(declaration, loaded):
    sources = {
        'tests.package.__init__': f'from {PROVIDER} import env as current\n{declaration}',
        'tests.package.consumer': f'from {PROVIDER} import legacy_retrieve_env\nenv = legacy_retrieve_env',
        'tests.future': "from tests.package import *\nif 'consumer' in globals():\n"
                        '    hidden = consumer.env',
    }
    expected = [('tests.future', 3, 'env')] if loaded else []
    assert implicit_legacy_imports(sources) == expected


@pytest.mark.parametrize('historical', [False, True])
@pytest.mark.parametrize('reverse', [False, True])
@pytest.mark.parametrize('forwarded', [False, True])
def test_fixture_import_gate_prefers_bound_package_aliases_in_star_exports(
        historical, reverse, forwarded):
    selection = 'legacy_retrieve_env\nenv = legacy_retrieve_env' if historical else 'env'
    loader = 'from .middle import *' if forwarded else 'from . import current as consumer'
    sources = {
        'tests.package.__init__': f"{loader}\n__all__ = ['consumer']",
        'tests.package.middle': 'from . import current as consumer',
        'tests.package.current': f'from {PROVIDER} import {selection}',
        'tests.package.consumer': f'from {PROVIDER} import legacy_retrieve_env\nenv = legacy_retrieve_env',
        'tests.future': 'from tests.package import *\nhidden = consumer.env',
    }
    if reverse:
        sources = dict(reversed(list(sources.items())))
    expected = [('tests.future', 2, 'env')] if historical else []
    assert implicit_legacy_imports(sources) == expected


@pytest.mark.parametrize('historical', [False, True])
def test_fixture_import_gate_loads_unbound_package_children_selected_by_star_exports(historical):
    selection = 'legacy_retrieve_env\nenv = legacy_retrieve_env' if historical else 'env'
    sources = {
        'tests.package.__init__': "__all__ = ['consumer']",
        'tests.package.consumer': f'from {PROVIDER} import {selection}',
        'tests.future': 'from tests.package import *\nhidden = consumer.env',
    }
    expected = [('tests.future', 2, 'env')] if historical else []
    assert implicit_legacy_imports(sources) == expected


@pytest.mark.parametrize('historical', [False, True])
@pytest.mark.parametrize('loaded', [False, True])
def test_fixture_import_gate_initializes_consumer_parent_packages_before_star_exports(historical, loaded):
    selection = 'legacy_retrieve_env\nenv = legacy_retrieve_env' if historical else 'env'
    sources = {
        'tests.entry.__init__': 'import tests.package.consumer as _private' if loaded else '',
        'tests.entry.future': "from tests.package import *\nif 'consumer' in globals():\n"
                              '    hidden = consumer.env',
        'tests.package.__init__': f'from {PROVIDER} import env as current',
        'tests.package.consumer': f'from {PROVIDER} import {selection}',
    }
    expected = [('tests.entry.future', 3, 'env')] if loaded and historical else []
    assert implicit_legacy_imports(sources) == expected


@pytest.mark.parametrize('declaration, error_line', [
    ("class Local:\n    __all__.append('env')", 4),
    ("class Local:\n    global __all__\n    __all__ = ['env']", 5),
    ("class Local:\n    class Nested:\n        __all__.append('env')", 5),
    ("class Local[T]:\n    __all__.extend(['env'])", 4),
    ("class Local:\n    __all__: list[str]\n    __all__.append('env')", 5),
    ("class Local:\n    __all__.append('env')\n    __all__ = []", 4),
    ("class Local:\n    __all__ = []\n    del __all__\n    __all__.append('env')", 6),
    ("class Local:\n    __all__ = []\n    __all__.append('env')", None),
    ("class Local[T]:\n    __all__ = []\n    __all__.append('env')", None),
    ("def unused():\n    __all__.append('env')", None),
    ("class Local:\n    def unused(self):\n        __all__.append('env')", None),
])
def test_fixture_import_gate_rejects_module_export_mutation_in_executed_star_source_classes(
        declaration, error_line):
    sources = {
        'tests.consumer': f'from {PROVIDER} import legacy_retrieve_env as env\n'
                          f'__all__ = []\n{declaration}',
        'tests.future': 'from tests.consumer import *',
    }
    if error_line:
        with pytest.raises(ValueError, match=rf'tests\.consumer:{error_line}:.*__all__'):
            implicit_legacy_imports(sources)
    else:
        assert implicit_legacy_imports(sources) == []


@pytest.mark.parametrize('declaration, error_line', [
    ("def unused(value=__all__.append('env')):\n    pass", 3),
    ("def unused(*, value=__all__.append('env')):\n    pass", 3),
    ("@(__all__.append('env') or (lambda value: value))\ndef unused():\n    pass", 3),
    ("class Local((__all__.append('env') or object)):\n    pass", 3),
    ("class Local(metaclass=(__all__.append('env') or type)):\n    pass", 3),
    ('def unused(__all__):\n    pass', None),
    ('def unused[__all__](value):\n    pass', None),
    ("class Local:\n    __all__ = []\n"
     "    def method(self, value=__all__.append('env')):\n        pass", None),
])
def test_fixture_import_gate_rejects_module_export_mutation_in_star_source_definition_headers(
        declaration, error_line):
    sources = {
        'tests.consumer': f'from {PROVIDER} import legacy_retrieve_env as env\n'
                          f'__all__ = []\n{declaration}',
        'tests.future': 'from tests.consumer import *',
    }
    if error_line:
        with pytest.raises(ValueError, match=rf'tests\.consumer:{error_line}:.*__all__'):
            implicit_legacy_imports(sources)
    else:
        assert implicit_legacy_imports(sources) == []


@pytest.mark.parametrize('declaration, postponed, error_line', [
    ("def unused(value: (__all__.append('env') or int)):\n    pass", False, 3),
    ("def unused(value: (__all__.append('env') or int), /):\n    pass", False, 3),
    ("def unused(*, value: (__all__.append('env') or int)):\n    pass", False, 3),
    ("def unused(*values: (__all__.append('env') or int)):\n    pass", False, 3),
    ("def unused(**values: (__all__.append('env') or int)):\n    pass", False, 3),
    ("def unused() -> (__all__.append('env') or int):\n    pass", False, 3),
    ("def unused[T](value: (__all__.append('env') or int)):\n    pass", False, 3),
    ("async def unused() -> (__all__.append('env') or int):\n    pass", False, 3),
    ('def unused[__all__](value: __all__) -> __all__:\n    pass', False, None),
    ('def unused(__all__: int) -> int:\n    pass', False, None),
    ("class Local:\n    __all__ = []\n"
     "    def method(self, value: (__all__.append('env') or int)):\n        pass", False, None),
    ("class Local:\n    __all__ = []\n"
     "    def method[T](self, value: (__all__.append('env') or int)):\n        pass", False, None),
    ("def unused(value: (__all__.append('env') or int)):\n    pass", True, None),
    ("def unused() -> (__all__.append('env') or int):\n    pass", True, None),
    ("def unused(value=__all__.append('env')):\n    pass", True, 4),
    ("def unused[__all__](value=__all__.append('env')):\n    pass", False, 3),
    ("class Local:\n    def method[T](self, value: (__all__.append('env') or int)):\n"
     "        pass\n    __all__ = []", False, 4),
])
def test_fixture_import_gate_respects_annotation_execution_for_star_exports(
        declaration, postponed, error_line):
    prefix = 'from __future__ import annotations\n' if postponed else ''
    sources = {
        'tests.consumer': f'{prefix}from {PROVIDER} import legacy_retrieve_env as env\n'
                          f'__all__ = []\n{declaration}',
        'tests.future': 'from tests.consumer import *',
    }
    if error_line:
        with pytest.raises(ValueError, match=rf'tests\.consumer:{error_line}:.*__all__'):
            implicit_legacy_imports(sources)
    else:
        assert implicit_legacy_imports(sources) == []


def frozen_retrieve(env):
    env.model.allowed = False
    response = fixtures.ask(env, intent='ask')
    assert response.status_code == 200, response.text
    saved = response.json()
    assert saved['turn']['receipt']['ask']['no_match'] is True
    assert env.model.calls == 0
    return env.http.app.state.ai_turn_store.get_request(saved['turn']['id'])['policy_versions']['retrieve']


def test_generic_fixture_uses_current_retrieve_in_real_http(default_env):
    assert ACTIVE['retrieve'] == '@7' and version('retrieve') == '@7'
    assert frozen_retrieve(default_env) == '@7'


@pytest.mark.parametrize('interrupted', [False, True])
def test_legacy_fixture_restores_nested_policy_context_after_teardown(tmp_path, interrupted):
    initial = dict(ACTIVE)
    assert version('retrieve') == '@7'
    lifetime = contextmanager(fixtures.legacy_retrieve_env.__wrapped__)
    with override(retrieve='@2'):
        try:
            with lifetime(tmp_path) as env:
                assert version('retrieve') == '@3'
                assert frozen_retrieve(env) == '@3'
                assert ACTIVE == initial
                if interrupted:
                    raise RuntimeError('synthetic fixture interruption')
        except RuntimeError as error:
            assert interrupted and str(error) == 'synthetic fixture interruption'
        assert version('retrieve') == '@2'
    assert version('retrieve') == '@7' and ACTIVE == initial


def test_generic_fixture_preserves_an_explicit_caller_override(tmp_path):
    lifetime = contextmanager(fixtures.env.__wrapped__)
    with override(retrieve='@2'):
        with lifetime(tmp_path) as env:
            assert version('retrieve') == '@2'
            assert frozen_retrieve(env) == '@2'
        assert version('retrieve') == '@2'
    assert version('retrieve') == '@7'
