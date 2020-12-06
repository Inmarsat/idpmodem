from setuptools import setup, find_packages


def readme():
    with open('README.rst') as readme_file:
        return readme_file.read()


setup(name='idpmodem',
      version='2.0.0',
      description='An API for interfacing to an Inmarsat-certified '
                  'IsatData Pro modem using AT commands over serial',
      url='https://github.com/Inmarsat/idpmodem',
      author='G Bruce Payne',
      author_email='geoff.bruce-payne@inmarsat.com',
      license='Apache',
      packages=find_packages(),
      install_requires=[
          'pyserial>=3.4',
          'aioserial>=1.3',
      ],
      include_package_data=True,
      zip_safe=False)
