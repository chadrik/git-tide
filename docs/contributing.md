## Contributing to semver-demo

First of all, thank you for taking the time to contribute! 🎉

When contributing to [semver-demo](https://gitlab.com/chadrik/semver-demo), please first create an [issue](https://gitlab.com/chadrik/semver-demo/-/issues) to discuss the change you wish to make before making a change.

## Setting up your local development environment

- clone the repository using either https or ssh depending on your preference and then run the following commands
```bash
python -m venv venv
.\venv\Scripts\Activate
pip install -r requirements.txt
pre-commit install
```

## Before making a merge request

1. Fork [the repository](https://gitlab.com/chadrik/semver-demo).
1. Clone the repository from your Gitlab Fork.
1. Follow the `Setting up your local development environment` directions above.
1. Check out a new branch and add your modification.
1. Add test cases for all your changes.
1. Run `nox -s lint` and `nox -s unit_tests` to ensure you follow the coding style and the tests pass.
1. Optionally, update the `./README.md` and any other documentation in `docs`
1. If you have modified documentation run `nox -s docs -- --serve` and open it in a browser to ensure your changes are correct.
1. **Do not** create **ANY** tags in the project, they will be automatically handled.
1. Create a [merge request](https://gitlab.com/chadrik/semver-demo/-/merge_requests) 🙏

## How we use labels

TODO


### Issue life cycle

TODO

### Merge request life cycle

TODO