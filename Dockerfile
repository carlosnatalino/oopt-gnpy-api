# oopt-gnpy-libyang, a dependency of gnpy, publishes no wheel for older Pythons on
# linux/amd64, and pip cannot build it from source in this slim image.
FROM python:3.12-slim
COPY . /oopt-gnpy-api
WORKDIR /oopt-gnpy-api
RUN apt update; apt install -y git
RUN pip install .
RUN mkdir -p /opt/application/oopt-gnpy/autodesign
CMD [ "python", "./samples/rest_example.py" ]