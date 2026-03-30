import ast
import unittest
from pathlib import Path


MAIN = Path(__file__).resolve().parents[1] / 'main.py'


class CommandRegistrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tree = ast.parse(MAIN.read_text(encoding='utf-8'))
        cls.plugin = next(
            node for node in cls.tree.body if isinstance(node, ast.ClassDef) and node.name == 'YuanRedeemPlugin'
        )
        cls.methods = {
            node.name: node
            for node in cls.plugin.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }

    def _decorator_exprs(self, method_name: str) -> list[str]:
        node = self.methods[method_name]
        return [ast.unparse(decorator) for decorator in node.decorator_list]

    def test_user_commands_are_registered_via_filter_command(self):
        for method_name in [
            'bind_account',
            'unbind_account',
            'binding_status',
            'redeem_codes',
        ]:
            decorators = self._decorator_exprs(method_name)
            self.assertTrue(
                any(expr.startswith('filter.command(') for expr in decorators),
                f'{method_name} should be registered with filter.command',
            )
            self.assertNotIn(
                'filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)',
                decorators,
                f'{method_name} should not rely on framework-level private-message filtering',
            )

    def test_admin_commands_are_visible_commands_without_framework_admin_filter(self):
        for method_name in [
            'add_codes_command',
            'delete_code_command',
            'list_codes_command',
            'clear_codes_command',
        ]:
            decorators = self._decorator_exprs(method_name)
            self.assertTrue(
                any(expr.startswith('filter.command(') for expr in decorators),
                f'{method_name} should be registered with filter.command',
            )
            self.assertNotIn(
                'filter.permission_type(filter.PermissionType.ADMIN)',
                decorators,
                f'{method_name} should not rely on framework-level admin permission filtering',
            )
            self.assertNotIn(
                'filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)',
                decorators,
                f'{method_name} should not rely on framework-level private-message filtering',
            )

    def test_plugin_implements_runtime_permission_and_private_chat_guards(self):
        self.assertIn('_is_admin', self.methods)
        self.assertIn('_ensure_private_chat', self.methods)

    def test_admin_commands_call_runtime_admin_guard(self):
        for method_name in [
            'add_codes_command',
            'delete_code_command',
            'list_codes_command',
            'clear_codes_command',
        ]:
            method_src = ast.unparse(self.methods[method_name])
            self.assertIn('self._is_admin(event)', method_src)

    def test_all_user_facing_commands_call_runtime_private_chat_guard(self):
        for method_name in [
            'bind_account',
            'unbind_account',
            'binding_status',
            'redeem_codes',
            'add_codes_command',
            'delete_code_command',
            'list_codes_command',
            'clear_codes_command',
        ]:
            method_src = ast.unparse(self.methods[method_name])
            self.assertIn('self._ensure_private_chat(event)', method_src)

    def test_legacy_listener_entrypoints_are_removed(self):
        self.assertNotIn('handle_private_commands', self.methods)
        self.assertNotIn('handle_admin_commands', self.methods)
        self.assertNotIn('_strip_admin_command_prefix', self.methods)


if __name__ == '__main__':
    unittest.main()
