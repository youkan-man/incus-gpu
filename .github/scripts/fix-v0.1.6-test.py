from pathlib import Path

path = Path("tests/test.sh")
text = path.read_text()
start = "test_driver_override_write_watchdog_returns() {\n"
end = "\ntest_inspect_single_command_restores_vm() {\n"
start_pos = text.find(start)
if start_pos < 0:
    raise SystemExit("driver_override test start not found")
end_pos = text.find(end, start_pos)
if end_pos < 0:
    raise SystemExit("driver_override test end not found")
replacement = r'''test_driver_override_write_watchdog_returns() {
    local out
    out=$($TOOL bind-vfio 0000:03:00.0 --force --timeout 7 --dry-run 2>&1)
    assert_contains "$out" 'driver_override設定 (0000:03:00.0)' && \
        assert_contains "$out" '(watchdog=7s)'
}
'''
text = text[:start_pos] + replacement + text[end_pos + 1:]
path.write_text(text)
print("v0.1.6 regression test corrected")
