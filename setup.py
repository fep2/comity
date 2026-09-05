from setuptools import setup

setup(
    name='hrrr-ingest',
    version='0.1.0',
    py_modules=['cli', 'get_nei_tree'],
    install_requires=[
        'herbie-data',
        'pandas',
        'xarray',
        'cfgrib',
        'eccodes',
        'scikit-learn',
        'duckdb',
    ],
    entry_points={
        'console_scripts': [
            'hrrr-ingest=cli:main',
            'tree_setup=get_nei_tree:generate_hrrr_cache',
        ],
    },
)