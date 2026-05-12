def pytest_addoption(parser):
    parser.addoption(
        '--update-baseline',
        action='store_true',
        default=False,
        help='Overwrite tests/baseline.json with current indicator values.',
    )
