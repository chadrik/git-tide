
get_tag() {
  echo $(cz bump "$@" --dry-run | grep tag | sed 's/tag to create: \(.*\)/\1/')
}

git remote get-url gitlab_origin &> /dev/null || {
  git config user.email "fake@email.com"
  git config user.name "ci-bot"
  URL=$(cut -d "@" -f2- <<< "$CI_REPOSITORY_URL")
  git remote add gitlab_origin "https://oauth2:$ACCESS_TOKEN@$URL"
}
