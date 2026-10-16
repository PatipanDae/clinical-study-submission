# Sudoku

A simple, self-contained Sudoku web application built with plain HTML, CSS, and JavaScript. No build step, no dependencies, no internet connection required — everything lives in a single `sudoku.html` file.

## Features

- **Random puzzle generator** — every game is a freshly generated board created with a backtracking algorithm. Each generated puzzle is verified to have a *unique* solution.
- **Three difficulty levels:**
  - Easy — 45 clues
  - Medium — 36 clues
  - Hard — 28 clues
- **Two ways to play:**
  - Click a cell, then click a number on the on-screen numpad.
  - Use your keyboard (see shortcuts below).
- **Smart highlighting** — selecting a cell highlights its row, column, and 3×3 box to make solving easier.
- **Check** — flags any incorrect entries in red without revealing the answer.
- **Solve** — reveals the full solution.
- **Reset** — clears your entries while keeping the original clues.
- **Auto win detection** — the game congratulates you when the board is completed correctly.
- **Responsive dark theme** — works on both desktop and mobile.

## Getting Started

No installation or build is required.

1. Download or clone this project.
2. Open `sudoku.html` in any modern web browser.

You can do this by:

- Double-clicking the `sudoku.html` file, **or**
- Running one of the following from a terminal:

  ```bash
  # macOS
  open sudoku.html

  # Linux
  xdg-open sudoku.html

  # Windows
  start sudoku.html
  ```

## How to Play

1. Choose a difficulty from the dropdown and click **New Game** (a Medium game starts automatically on load).
2. Click any empty cell to select it.
3. Enter a number using the numpad or your keyboard.
4. Fill the grid so that every row, column, and 3×3 box contains the digits 1–9 exactly once.

### Controls

| Action            | Mouse / Touch                   | Keyboard                         |
|-------------------|---------------------------------|----------------------------------|
| Select a cell     | Click the cell                  | Arrow keys to move selection     |
| Enter a number    | Click a numpad button (1–9)     | Press `1`–`9`                    |
| Erase a cell      | Click the `⌫` button            | `Backspace`, `Delete`, or `0`    |

### Buttons

| Button      | Description                                                  |
|-------------|--------------------------------------------------------------|
| Difficulty  | Choose Easy, Medium, or Hard for the next new game.          |
| New Game    | Generates a fresh puzzle at the selected difficulty.         |
| Check       | Highlights incorrect entries in red.                         |
| Solve       | Reveals the complete solution.                               |
| Reset       | Clears your entries but keeps the original clues.            |

Fixed clue cells cannot be edited.

## How It Works

The app is a single HTML file with embedded CSS and JavaScript:

- **Grid generation** fills an empty 9×9 grid using randomized backtracking to produce a valid, complete solution.
- **Puzzle creation** removes cells one at a time, each time confirming (via a solution counter capped at 2) that the puzzle still has exactly one solution. Cells are removed until the target clue count for the chosen difficulty is reached.
- **Validation** checks each entry against the stored solution for the Check and win-detection features.

## Project Structure

```
.
├── sudoku.html    # The entire application (HTML + CSS + JavaScript)
└── README.md      # This file
```

## Browser Support

Works in any modern browser that supports CSS Grid and ES6 JavaScript (Chrome, Firefox, Safari, Edge).

## License

Free to use and modify.
