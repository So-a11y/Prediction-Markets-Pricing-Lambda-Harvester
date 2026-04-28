from setuptools import setup, find_packages

setup(
    name="lambda-harvester",
    version="0.1.0",
    packages=find_packages(),
    install_requires=[
        "numpy>=1.26",
        "scipy>=1.12",
        "requests>=2.31",
        "pandas>=2.2",
    ],
    entry_points={
        "console_scripts": [
            "lambda-harvester=lambda_harvester.main:main",
        ],
    },
    python_requires=">=3.11",
)
