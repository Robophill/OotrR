"""Mouse wheel scrolling of the GUI's pages."""
import _src_path  # noqa: F401  (src on sys.path)
import unittest
from unittest.mock import MagicMock, patch

from oot_rando.gui import theme
from oot_rando.gui.randomizer_gui import Application, ECONOMY_OPTION
from oot_rando.logic import rando_options
from oot_rando.patching.item_replacements import ECONOMY_PRESETS, economy_preset_tooltip


class WheelTests(unittest.TestCase):
    def test_x11_buttons_scroll(self):
        self.assertIn('<Button-4>', theme.WHEEL_EVENTS)
        self.assertIn('<Button-5>', theme.WHEEL_EVENTS)
        self.assertEqual(theme.wheel_units(4, 0), -1)
        self.assertEqual(theme.wheel_units(5, 0), 1)

    def test_mouse_wheel_delta(self):
        self.assertEqual(theme.wheel_units('??', 120), -1)
        self.assertEqual(theme.wheel_units('??', -240), 2)
        # Small deltas (macOS, touchpads) still move a line.
        self.assertEqual(theme.wheel_units('??', 3), -1)
        self.assertEqual(theme.wheel_units('??', -1), 1)
        self.assertEqual(theme.wheel_units('??', 0), 0)

    def test_wheel_never_changes_a_setting(self):
        import tkinter as tk
        from tkinter import ttk
        try:
            root = tk.Tk()
        except tk.TclError as error:
            self.skipTest(str(error))
        try:
            root.withdraw()
            values = ('One', 'Two', 'Three')
            combo = ttk.Combobox(root, values=values, state='readonly')
            combo.set('Two')
            number = tk.IntVar(root, 5)
            spin = ttk.Spinbox(root, from_=1, to=10, textvariable=number)
            theme.keep_values_on_wheel(root)
            for widget_class in ('TCombobox', 'TSpinbox'):
                for sequence in theme.WHEEL_EVENTS:
                    self.assertEqual(root.bind_class(widget_class, sequence), '')
            for widget in (combo, spin):
                widget.pack()
            root.update()
            for widget in (combo, spin):
                widget.event_generate('<MouseWheel>', delta=-120, x=5, y=5)
            root.update()
            self.assertEqual(combo.get(), 'Two')
            self.assertEqual(number.get(), 5)
        finally:
            root.destroy()


class ChoiceRowTests(unittest.TestCase):
    class Variable:
        def __init__(self, value=''):
            self.value = value

        def get(self):
            return self.value

        def set(self, value):
            self.value = value

    def test_economy_and_full_choices_keep_separate_values(self):
        app = Application.__new__(Application)
        app.label_cell = MagicMock(return_value=(MagicMock(), MagicMock()))
        app.fonts = object()
        app.full_vars = {}
        app.economy_preset = self.Variable('vanilla')
        app.economy_display = self.Variable(ECONOMY_PRESETS['vanilla'].label)
        option = next(option for option in rando_options.OPTIONS if option.name == 'item_pool_value')
        economy_widget, option_widget = MagicMock(), MagicMock()

        with patch('oot_rando.gui.randomizer_gui.tk.StringVar', side_effect=self.Variable), \
                patch('oot_rando.gui.randomizer_gui.ttk.Combobox',
                      side_effect=[economy_widget, option_widget]) as combobox, \
                patch('oot_rando.gui.randomizer_gui.theme.Tooltip') as tooltip, \
                patch('oot_rando.gui.randomizer_gui.theme.rule') as rule:
            app.option_row(object(), 0, ECONOMY_OPTION, variable=app.economy_preset,
                           display=app.economy_display, tip=economy_preset_tooltip())
            app.option_row(object(), 1, option)

        self.assertEqual(combobox.call_args_list[0].kwargs['values'],
                         [preset.label for preset in ECONOMY_PRESETS.values()])
        self.assertEqual(tooltip.call_args_list[0].args[1], economy_preset_tooltip())
        self.assertEqual(rule.call_count, 1)
        app.economy_display.set(ECONOMY_PRESETS['gold_rush'].label)
        economy_selected = next(call.args[1] for call in economy_widget.bind.call_args_list
                                if call.args[0] == '<<ComboboxSelected>>')
        economy_selected()
        self.assertEqual(app.economy_preset.get(), 'gold_rush')
        self.assertNotIn('economy_preset', app.full_vars)
        self.assertEqual(app.full_vars[option.name].get(), option.default)

        selected_value, selected_label, _ = next(
            (value, label, reason) for value, label, reason in option.values
            if reason is None and value != option.default)
        app.full_vars[option.name].display.set(selected_label)
        option_selected = next(call.args[1] for call in option_widget.bind.call_args_list
                               if call.args[0] == '<<ComboboxSelected>>')
        option_selected()
        self.assertEqual(app.full_vars[option.name].get(), selected_value)
        self.assertEqual(app.economy_preset.get(), 'gold_rush')
        economy_widget.bind.assert_any_call('<MouseWheel>', unittest.mock.ANY)
        option_widget.unbind_class.assert_called_once_with('TCombobox', '<MouseWheel>')


if __name__ == '__main__':
    unittest.main()
