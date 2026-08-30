# Fixture for SC-008: proves the shell check actually fails a build that gains a shell.
FROM gcr.io/distroless/base-debian12:nonroot@sha256:7f0c72cd138b442ae0deeb69c08b1acf5525439ba251a49ad93c320a061567e5
COPY --from=docker.io/library/busybox:1.36-musl@sha256:3c6ae8008e2c2eedd141725c30b20d9c36b026eb796688f88205845ef17aa213 /bin/sh /bin/sh
ENTRYPOINT ["/bin/true"]
