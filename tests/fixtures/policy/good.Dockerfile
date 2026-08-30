# Fixture control: a compliant minimal image.
FROM gcr.io/distroless/base-debian12:nonroot@sha256:7f0c72cd138b442ae0deeb69c08b1acf5525439ba251a49ad93c320a061567e5
ENTRYPOINT ["/busybox/true"]
