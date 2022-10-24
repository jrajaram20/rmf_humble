from setuptools import setup

package_name = 'om_fleet_qm'

setup(
    name=package_name,
    version='0.0.0',
    packages=[package_name],
    data_files=[
        ('share/ament_index/resource_index/packages',
            ['resource/' + package_name]),
        ('share/' + package_name, ['package.xml']),
    ],
    install_requires=['setuptools'],
    zip_safe=True,
    maintainer='rajaram',
    maintainer_email='jrajaram20@gmail.com',
    description='TODO: Package description',
    license='TODO: License declaration',
    tests_require=['pytest'],
    entry_points={
        'console_scripts': [
            'fleet_adapter=om_fleet_qm.fleet_adapter:main',
            'fleet_manager=om_fleet_qm.fleet_manager:main'
        ],
    },
)