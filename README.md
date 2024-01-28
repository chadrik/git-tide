
## Running the demo

```commandline
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
./demo.sh
```

## Problems

- In a scenario where this runs as part of a merge request pipeline, it will create a version bump commit after every merged MR, which is a bit noisy
- Adding the version to a config file like `pyproject.toml` will inevitably create merge conflicts, and it's not safe to simply always take "theirs".  Storing it in a dedicated `VERSION` file would make conflicts with that file safe to ignore.  

Both of these problems could be solved by only storing the version in a git tag, and then read by a script running the build process (e.g. `python setup.py` or `rez release`).
