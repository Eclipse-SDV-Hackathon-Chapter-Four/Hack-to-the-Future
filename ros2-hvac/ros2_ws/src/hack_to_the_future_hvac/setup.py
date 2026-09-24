"""ament_python packaging for the HVAC simulator.

Installed artifacts:

* console script ``hvac_simulator`` (the ROS 2 node)
* ``launch/hvac.launch.py`` (what Eclipse Muto launches)
* ``config/hvac.dbc`` (the CAN frame contract, loaded by the node at runtime)
"""

from setuptools import find_packages, setup

package_name = "hack_to_the_future_hvac"

setup(
    name=package_name,
    version="0.1.0",
    packages=find_packages(exclude=["test"]),
    data_files=[
        ("share/ament_index/resource_index/packages", [f"resource/{package_name}"]),
        (f"share/{package_name}", ["package.xml"]),
        (f"share/{package_name}/launch", ["launch/hvac.launch.py"]),
        (f"share/{package_name}/config", ["config/hvac.dbc"]),
    ],
    install_requires=["setuptools"],
    zip_safe=True,
    maintainer="Hack to the Future",
    maintainer_email="devnull@example.com",
    description="ROS 2 HVAC simulator with CAN bridge, deployed by Eclipse Muto.",
    license="EPL-2.0",
    entry_points={
        "console_scripts": [
            "hvac_simulator = hack_to_the_future_hvac.hvac_simulator:main",
        ],
    },
)
