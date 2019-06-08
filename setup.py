from setuptools import setup


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
      packages=[
          'idpmodem',
      ],
      install_requires=[
          'pyserial>=3.4',
          'headless',
      ],
      include_package_data=True,
      classifiers=[
          'Programming Language :: Python :: 2 :: Only',
          'Intended Audience :: Developers',
          'Operating System :: Microsoft :: Windows',
          'Operating System :: POSIX',
          'Environment :: Console',
      ],
      zip_safe=False)
