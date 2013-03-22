from setuptools import setup
setup(
    name="zmq_irc",
    packages=["zmq_irc"],
    version="0.1",
    description="IRC / ZMQ bridge",
    author="Nell Hardcastle",
    author_email="chizu@spicious.com",
    install_requires=["pyzmq>=2.1.7",
                      "txzmq",
                      "twisted"]
)
