# Contributing to Safe Drive Ejector Tool

Thank you for your interest in contributing to **Safe Drive Ejector Tool**! We welcome community contributions to help improve disk management, safety, performance, and user experience.

---

## Code of Conduct

By participating in this project, you agree to abide by our [Code of Conduct](CODE_OF_CONDUCT.md).

---

## How Can I Contribute?

### Reporting Bugs
Before submitting an issue, please:
1. Search existing GitHub Issues to see if the bug has already been reported.
2. Verify that you are running the latest version of the tool.
3. Include detailed steps to reproduce, your operating system and version, and any relevant terminal or console logs.

### Feature Requests
Feature suggestions are welcome! Please open an issue outlining:
- The problem you are trying to solve.
- Your proposed solution or user experience.
- Any alternative solutions or workarounds considered.

### Pull Requests
1. Fork the repository on GitHub: [https://github.com/siraj-bd/Safe-Drive-Ejector-macOS](https://github.com/siraj-bd/Safe-Drive-Ejector-macOS).
2. Clone your fork locally:
   ```bash
   git clone https://github.com/<your-username>/Safe-Drive-Ejector-macOS.git
   cd Safe-Drive-Ejector-macOS
   ```
3. Create a descriptive feature branch:
   ```bash
   git checkout -b feature/your-feature-name
   ```
4. Follow the coding guidelines below and make your changes.
5. Run tests locally to ensure everything passes:
   ```bash
   python3 -m unittest discover tests
   ```
6. Commit your changes with clear, concise commit messages.
7. Push to your fork and submit a Pull Request to `main`.

---

## Development & Coding Standards

### Project Structure
- `core/`: Pure Python core engine, data models, state manager, and configuration.
- `platform_adapters/`: macOS disk query, mount, and eject implementations (`macos.py`).
- `ui/SafeEjectMenuBar.swift`: Native macOS Menu Bar status item and WKWebView panel host.
- `ui/components/SafeDriveEjectorCard.html`: HTML5/CSS3/JavaScript responsive control card.
- `tests/`: Automated test suite.

### Python Guidelines
- Adhere to PEP 8 standards.
- Maintain type hints for function signatures.
- Do not add heavy external dependencies unless strictly necessary (keep core lightweight with zero required pip installs).

### Swift Guidelines
- Ensure compatibility with macOS 12.0+ (Monterey, Ventura, Sonoma, Sequoia, Golden Gate & all modern versions).
- Maintain efficient ARC memory management and clean async/main thread UI dispatching.

---

## License

By contributing to Safe Drive Ejector Tool, you agree that your contributions will be licensed under the project's [MIT License](LICENSE).
