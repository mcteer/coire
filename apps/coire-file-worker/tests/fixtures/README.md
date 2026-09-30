# PDF parser fixture

These minimal fixtures come from PDFium's `testing/resources/` directory:

- [`encrypted_hello_world_r3.pdf`](https://pdfium.googlesource.com/pdfium/+/refs/heads/chromium/7635/testing/resources/encrypted_hello_world_r3.pdf), 1,033 bytes, SHA-256 `cdbfd4f6728786dc70c7514bd5c669a38b03bba38be3e91ec46ccb02168b9ad3`.
- [`hello_world.pdf`](https://pdfium.googlesource.com/pdfium/+/refs/heads/main/testing/resources/hello_world.pdf), 840 bytes, SHA-256 `1e06a6e12329f0c3760680a70baea126e040e66e0bf34d3b14a71863096ee1e3`.
PDFium's [LICENSE](PDFIUM-LICENSE) is included with its redistribution notice. The fixtures verify Unicode extraction
and the password-specific error path; their bytes are never included in a runtime image.
