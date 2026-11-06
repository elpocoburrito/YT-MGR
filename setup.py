from setuptools import find_packages, setup

setup(
    name="ytmgr",
    version="0.1.0",
    packages=find_packages(),
    include_package_data=True,
    description="CLI tool to manage YouTube playlists",
    long_description=open("README.md", encoding="utf-8").read(),
    long_description_content_type="text/markdown",
    author="ytmgr",
    license="GPL-3.0-or-later",
    python_requires=">=3.10",
    install_requires=[
        "google-api-python-client>=2.135.0",
        "google-auth-httplib2>=0.1.1",
        "google-auth-oauthlib>=1.2.1",
        "python-dotenv>=1.0.1",
    ],
    entry_points={
        "console_scripts": [
            "ytmgr=ytmgr.cli:main",
        ],
    },
    classifiers=[
        "Programming Language :: Python :: 3.10",
        "License :: OSI Approved :: GNU General Public License v3 or later (GPLv3+)",
    ],
)