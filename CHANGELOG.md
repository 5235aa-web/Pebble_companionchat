# Changelog

All notable releases for Pebble Companion Chat are recorded here.
Each `## x.y.z` section is used by `npm run release -- x.y.z` as both the
GitHub Release description and the in-app update panel notes.

## 0.3.0

- Added support for RisuAI shorthand `<img="...">` tags in CHARX greetings and chat messages so bundled character expressions and inline images render properly instead of being stripped.
- Deferred greeting image parsing and loading until the empty-stage greeting view is visible and switched to a greeting containing images, reducing unnecessary rendering work.
- Updated `README.md` Section 3 (Character Cards) with documentation on CHARX containers, bundled asset extraction, inline media, and the Stage HUD.
- Fixed single-chat token mismatches between the reply footer and the top context meter by persisting branch-level provider usage in SQLite, separating latest context size from cumulative continuation billing, and refreshing context counts after swipe, edit, and delete actions.

## 0.2.0

- After completing the import of the CHARX cards and enabling the usage mechanism, now CHARX card import is supported, along with the display of its complete media resources, including pictures, audio, and videos, which can all be viewed in the character card editing window. These resources are lazy-loaded, so they will not affect the rendering. 
- Additionally, the issue of unable to immediately display attachment pictures in the front-end chat interface has been fixed.

## 0.1.0

- Initial release of Pebble Companion Chat (P.C.C).
